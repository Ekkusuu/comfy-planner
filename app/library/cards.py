"""Card storage: one row per card in SQLite, with the card as JSON.

A card is `draft` (written by the LLM, not checked) or `approved` (checked by you). Re-generating a card that
already exists doesn't touch it: the new version waits in `pending` until you accept or reject it.
"""
from pydantic import BaseModel, ValidationError

from app.config import Config
from app.db import Database, dumps, now
from app.library.card_models import CARD_MODELS


class CardError(Exception):
    """A card request that can't be done; the message is meant for the user."""


class CardStore:
    def __init__(self, db: Database, config: Config):
        self.db, self.config = db, config

    def _model(self, kind: str) -> type[BaseModel]:
        if kind not in CARD_MODELS:
            raise KeyError(kind)
        return CARD_MODELS[kind]

    # ---------- read ----------

    def _filled(self, kind: str, row: dict | None) -> dict | None:
        """A card stored before a field existed lacks it. Read it through the model so every card has the full shape
        the editor binds to (a missing list used to stop the LoRA editor from drawing at all). A stored card that no
        longer validates is returned as it is."""
        if row is None:
            return None
        for column in ("card", "pending"):
            if row.get(column) is not None:
                try:
                    row[column] = self._model(kind).model_validate(row[column]).model_dump()
                except ValidationError:
                    pass
        return row

    def get(self, kind: str, key: str) -> dict | None:
        return self._filled(kind, self.db.row("SELECT * FROM cards WHERE kind = ? AND key = ?", (kind, key)))

    def of_kind(self, kind: str) -> list[dict]:
        return [self._filled(kind, r) for r in self.db.rows("SELECT * FROM cards WHERE kind = ? ORDER BY key", (kind,))]

    def usable(self, kind: str) -> list[dict]:
        """Cards the planner may use: approved ones, plus drafts if `allow_draft_cards` is on."""
        return [c for c in self.of_kind(kind) if c["status"] == "approved" or self.config.allow_draft_cards]

    # ---------- write ----------

    def save_built(self, kind: str, key: str, card: BaseModel) -> dict:
        """Store a freshly built card. A first card becomes a draft; for an existing card the new one waits as pending."""
        existing = self.get(kind, key)
        if existing is None:
            self.db.execute("INSERT INTO cards (kind, key, status, card, pending, created_at, updated_at) VALUES (?,?,?,?,NULL,?,?)",
                            (kind, key, "draft", dumps(card.model_dump()), now(), now()))
        else:
            self.db.execute("UPDATE cards SET pending = ?, updated_at = ? WHERE kind = ? AND key = ?",
                            (dumps(card.model_dump()), now(), kind, key))
        return self.get(kind, key)

    def save_edit(self, kind: str, key: str, data: dict) -> dict:
        """Replace the card with the user's edited version (validated). The status stays as it was."""
        self._require(kind, key)
        try:
            card = self._model(kind).model_validate(data)
        except ValidationError as e:
            raise CardError("The card isn't valid: " + "; ".join(f'{".".join(str(p) for p in err["loc"])}: {err["msg"]}'
                                                                 for err in e.errors()[:5])) from e
        self.db.execute("UPDATE cards SET card = ?, updated_at = ? WHERE kind = ? AND key = ?",
                        (dumps(card.model_dump()), now(), kind, key))
        return self.get(kind, key)

    def approve(self, kind: str, key: str) -> dict:
        self._require(kind, key)
        self.db.execute("UPDATE cards SET status = 'approved', updated_at = ? WHERE kind = ? AND key = ?", (now(), kind, key))
        return self.get(kind, key)

    def accept_pending(self, kind: str, key: str) -> dict:
        """Take the re-generated card. It's LLM text again, so the card goes back to draft until you approve it."""
        row = self._require(kind, key)
        if row["pending"] is None:
            raise CardError("There is no re-generated card waiting.")
        self.db.execute("UPDATE cards SET card = pending, pending = NULL, status = 'draft', updated_at = ? WHERE kind = ? AND key = ?",
                        (now(), kind, key))
        return self.get(kind, key)

    def reject_pending(self, kind: str, key: str) -> dict:
        self._require(kind, key)
        self.db.execute("UPDATE cards SET pending = NULL, updated_at = ? WHERE kind = ? AND key = ?", (now(), kind, key))
        return self.get(kind, key)

    def _require(self, kind: str, key: str) -> dict:
        self._model(kind)
        row = self.get(kind, key)
        if row is None:
            raise KeyError(key)
        return row
