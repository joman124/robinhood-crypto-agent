"""The Claude Code skills in ``.claude/skills/rhca-*``.

Every session that operates this agent follows these files, so a skill that
names a command the CLI does not have sends Claude down a path that fails, or
one it improvises. What these pin down: each skill's frontmatter parses, its
``name`` is its folder's and its ``description`` is set; and every
``rhca <subcommand>`` it mentions is one the parser ``rhca`` builds has.

In code (fenced blocks and inline spans) the check is strict: the first word
after ``rhca`` must be a subcommand, and every flag after it one of that
subcommand's. Prose is looser, so ``rhca run's proposals`` reads as prose: a
word there is flagged only when it looks like a command -- a near miss of a
subcommand's name, or followed by a flag. The vendored ``backtest-expert``
skill is not ours and is not checked.
"""

from __future__ import annotations

import argparse
import difflib
import re
import shlex
from pathlib import Path

import pytest
import yaml

from robinhood_crypto_agent.cli import build_parser

SKILLS = Path(__file__).resolve().parents[2] / ".claude" / "skills"
VENDORED = {"backtest-expert"}
OURS = {"rhca-status", "rhca-review-proposal", "rhca-forward-test"}
SKILL_FILES = sorted(
    path for path in SKILLS.glob("rhca-*/SKILL.md") if path.parent.name not in VENDORED
)

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
FENCE = ("```", "~~~")
#: A shell comment: ``#`` opening the line or after whitespace.
COMMENT = re.compile(r"(?:^|(?<=\s))#")
INLINE = re.compile(r"`([^`]+)`")
#: ``rhca`` as a command word (``.venv\Scripts\rhca`` too), not ``rhca-status``.
RHCA = re.compile(r"(?<![\w.-])rhca(?:\.exe)?(?=\s|$)")
#: Where a shell command ends: a pipe, ``&&``, ``;`` or a redirect.
COMMAND_END = re.compile(r"\s(?:\|\|?|&&|;|>>?)(?=\s|$)")
FLAG = re.compile(r"^--?[A-Za-z]")
PROSE = re.compile(r"(?<![\w./-])rhca\s+([a-z][a-z0-9-]*)(\s+--?[A-Za-z])?")


def cli_surface() -> tuple[dict[str, bool], dict[str, set[str]]]:
    """The global options (each: does it take a value?) and every subcommand
    with its options, from the parser ``rhca`` itself runs."""
    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    global_options = {
        option: action.nargs != 0 for action in parser._actions for option in action.option_strings
    }
    commands = {name: set(p._option_string_actions) for name, p in sub.choices.items()}
    return global_options, commands


def split_markdown(text: str) -> tuple[list[str], str]:
    """The code fragments (fenced lines, inline spans) and the prose of a
    Markdown file. A shell comment in a fenced block is prose."""
    code: list[str] = []
    outside: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(FENCE):
            fenced = not fenced
            outside.append("")
        elif fenced:
            command, *comment = COMMENT.split(line, maxsplit=1)
            code.append(command)
            outside.append(comment[0] if comment else "")
        else:
            outside.append(line)
    rest = "\n".join(outside)
    return code + INLINE.findall(rest), INLINE.sub(" ", rest)


def _code_problems(fragment: str, global_options, commands) -> list[str]:
    problems = []
    for match in RHCA.finditer(fragment):
        rest = COMMAND_END.split(fragment[match.end():], maxsplit=1)[0]
        try:
            tokens = iter(shlex.split(rest))
        except ValueError:
            tokens = iter(rest.split())
        command = None
        for token in tokens:
            option = token.split("=", 1)[0]
            if option in global_options:
                if global_options[option] and "=" not in token:
                    next(tokens, None)
                continue
            command = token
            break
        if command is None:  # `rhca` named on its own
            continue
        if command not in commands:
            problems.append(f"`rhca {command}` is not a subcommand ({fragment.strip()!r})")
            continue
        for token in tokens:
            option = token.split("=", 1)[0]
            if FLAG.match(token) and option not in commands[command] | set(global_options):
                problems.append(f"`rhca {command}` has no option {option} ({fragment.strip()!r})")
    return problems


def _prose_problems(prose: str, commands) -> list[str]:
    problems = []
    for match in PROSE.finditer(prose):
        word, flag = match.groups()
        if word in commands:
            continue
        inflected = any(word.startswith(name) for name in commands)  # "runs", "reports"
        near = difflib.get_close_matches(word, commands, n=1, cutoff=0.8)
        if flag or (near and not inflected):
            problems.append(f"prose names `rhca {word}`, which is not a subcommand")
    return problems


def command_problems(text: str) -> list[str]:
    """Every ``rhca`` mention in ``text`` that names no real subcommand or option."""
    global_options, commands = cli_surface()
    code, prose = split_markdown(text)
    problems = []
    for fragment in code:
        problems += _code_problems(fragment, global_options, commands)
    return problems + _prose_problems(prose, commands)


def test_our_skills_are_found_and_the_vendored_one_is_not() -> None:
    names = {path.parent.name for path in SKILL_FILES}
    assert OURS <= names
    assert not names & VENDORED


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_frontmatter_names_its_folder_and_describes_the_skill(path: Path) -> None:
    match = FRONTMATTER.match(path.read_text(encoding="utf-8"))
    assert match, "SKILL.md must open with a --- YAML frontmatter block"
    meta = yaml.safe_load(match.group(1))
    assert isinstance(meta, dict)
    assert meta.get("name") == path.parent.name
    description = meta.get("description")
    assert isinstance(description, str) and description.strip()
    assert isinstance(meta.get("argument-hint", ""), str)


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_every_rhca_command_a_skill_names_is_real(path: Path) -> None:
    assert command_problems(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "text",
    [
        "`rhca run`'s proposals are in the audit log",
        "rhca run's proposals, and rhca runs every minute",
        "The CLI (`rhca`) holds the strategy; rhca is not a broker.",
        "```\n.venv\\Scripts\\rhca status   # rhca is read-only here\n```",
        "`rhca --data-dir data audit --proposal-id <id>`",
        '```\nrhca approve <id> --approval "execute <id>" --quote fresh.json\n```',
        "`.claude/skills/rhca-status/SKILL.md`",
    ],
)
def test_the_check_reads_prose_and_commands_apart(text: str) -> None:
    assert command_problems(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "`rhca stauts`",
        "run rhca stauts first",
        "```\nrhca analyse -v\n```",
        "`rhca audit --proposal <id>`",
        "`rhca shadow --backfil`",
        "rhca frobnicate --now",
    ],
)
def test_the_check_catches_a_typo(text: str) -> None:
    assert command_problems(text)
