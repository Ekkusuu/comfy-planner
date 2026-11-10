"""A card stored before a field existed is read back with that field filled in (the LoRA editor binds to every list)."""
from app.db import dumps, now
from app.library.card_models import LoraCard
from app.library.cards import CardStore

OLD_CARD = {"file": "old.safetensors", "sha256": "ab", "family": "Anima", "kind": "style", "what_it_does": "A style.",
            "trigger_words": [], "style_tags": [], "tags": [], "weight": {"rec": 1, "min": 0.5, "max": 1.5}, "usage_notes": [],
            "civitai": None, "metadata_only": True, "notice": "", "needs_attention": [], "built_with": "", "built_at": ""}


def put(db, key, card, pending=None):
    db.execute("INSERT INTO cards (kind, key, status, card, pending, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
               ("lora", key, "approved", dumps(card), dumps(pending) if pending else None, now(), now()))


def test_a_card_stored_without_a_newer_field_gets_its_default(db, config):
    put(db, "old.safetensors", OLD_CARD, pending=OLD_CARD)
    cards = CardStore(db, config)
    assert "remove_from_negative" not in OLD_CARD
    for row in (cards.get("lora", "old.safetensors"), cards.of_kind("lora")[0]):
        assert row["card"]["remove_from_negative"] == [] and row["pending"]["remove_from_negative"] == []
        assert LoraCard.model_validate(row["card"]).file == "old.safetensors"


def test_a_card_that_no_longer_validates_is_returned_as_stored(db, config):
    broken = {"file": "bad.safetensors", "kind": "not-a-kind"}
    put(db, "bad.safetensors", broken)
    assert CardStore(db, config).get("lora", "bad.safetensors")["card"] == broken
