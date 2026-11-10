"""Synthetic placeholder rules for the content-rule tests.

The pipeline tests never use the real rules file: conftest.py loads the rules written here instead (through the same loader and
self-test the app uses), so every surface is exercised end to end with obviously made-up words. Group A and group B stand for the two
groups of rule `cse`; group B is also the one avoided on its own. Rule `zz` is a second, unrelated rule: it proves that a rule added
to the data file is applied everywhere with no code change. The real file is covered by tests/test_content_rules.py, which reads its
words from the file itself.

The lists hold single placeholder words; a test picks the ones it needs by position and joins them with ordinary words of its own.
"""
from pathlib import Path

CONTENT_TERMS = ["probea1", "probea2", "probea3", "probea4", "probea5", "probea6", "probea7"]       # group A, as typed in a text
SUBJECT_TERMS = ["probeb1", "probeb2", "probeb3", "probeb4", "probeb5", "probeb6", "probeb7", "probeb8"]   # group B
MODIFIERS = ["probem1", "probem2"]            # group B entries made of a modifier and a word: "probem1 girl"
NUMBERS = ["12", "13", "14", "15", "17"]        # ages that count under the age check
OTHER_A, OTHER_B = "probec1", "probed1"       # the two groups of rule `zz`

CSE_MESSAGE = "Blocked: test rule one."
ZZ_MESSAGE = "Blocked: test rule two."

RULES_TEXT = f"""
[[rule]]
code = 'cse'
message = '{CSE_MESSAGE}'
old_codes = ['old_cse']

[[rule.all_of]]
name = 'a'
words = ['probea1', 'probea2', 'probea3', 'probea4', 'probea5', 'probea6', 'probea7s?', 'probea8\\w*']

[[rule.all_of]]
name = 'b'
check = 'age_below_18'
avoid_alone = true
words = ['probeb1', 'probeb2', 'probeb3', 'probeb4', 'probeb5', 'probeb6', 'probeb7(?![\\s-]*(?:blue|pink))', 'probeb8',
  'probem1 girl', 'probem1 boy', 'probem2 girl', 'probem2 boy']

[[rule]]
code = 'zz'
message = '{ZZ_MESSAGE}'

[[rule.all_of]]
name = 'c'
words = ['probec1', 'probec2']

[[rule.all_of]]
name = 'd'
words = ['probed1', 'probed2']
"""


def write_rules(folder: Path, text: str = RULES_TEXT, name: str = "rules.toml") -> Path:
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path
