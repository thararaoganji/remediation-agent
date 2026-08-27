"""Fix-generation prompt for the coverage-enhancement agent — the coverage
equivalent of sonar/techdebt_agent/prompts.py's build_fix_prompt(). Kept in
this package (not the shared one) since it's a genuinely different task
shape: writing new test code against an existing production file, not
patching the production file itself."""

COVERAGE_SKELETON = """\
You are a test engineer raising JUnit test coverage for an existing, \
already-working production class in a Java/Spring Boot codebase.

PRODUCTION FILE: {file_path}
Current line coverage: {coverage:.1f}% — {uncovered_lines} uncovered line(s), \
{uncovered_conditions} uncovered branch(es)/condition(s).

PRODUCTION FILE CONTENT (read-only — you may not modify this file):
{file_content}

TEST FILE: {test_file_path}
{test_file_status}
{test_file_content}

REQUIREMENTS:
1. Do NOT modify the production file. Your only output is the test file.
2. Do NOT remove, rename, or change the behavior of any existing test method
   already in the test file — only ADD new test method(s).
3. Target the specific lines/branches described above: read the production
   file, identify which branches, conditionals, exception paths, or methods
   are least likely to already be exercised given the coverage numbers and
   the test file's current content, and write test(s) that exercise them.
4. Use the same test framework and style already used in the test file
   (or, if it doesn't exist yet, JUnit 5 + Mockito for a Spring class with
   dependencies — match this codebase's existing conventions where visible).
5. Tests must be deterministic and self-contained — no reliance on a real
   database, network call, wall-clock time, or external service unless the
   class already provides a seam (an injected mock) for it.
6. Only the production file above is shown to you in full — every OTHER
   type you reference (a DTO/entity field setter, an enum constant, a
   collaborator's constructor, a response object's accessor) is one you
   are NOT shown the real shape of. Do not invent a method name, enum
   constant, or constructor signature for those types — construct them
   only using members you can already see referenced in the production
   file's own code (a field type, a method's own parameter/return type, an
   import). If exercising a branch would require calling into a type whose
   real shape you can't see and can't safely infer this way, skip that
   branch rather than guessing at an API that may not exist.
7. If reaching a genuinely higher coverage would require infrastructure this
   prompt doesn't give you (e.g. a real database, a complex mock chain not
   inferable from this file alone, or the class is effectively untestable as
   structured), output NO_SAFE_FIX: <one-line reason> instead of guessing or
   writing a test that doesn't actually exercise anything meaningful.
{retry_block}
OUTPUT FORMAT:
Output the COMPLETE test file content — every line, from package declaration
to final closing brace, existing tests included unchanged, plus your new
test method(s) — wrapped in a single fenced code block and nothing else. Do
not output a diff or partial file.
"""

_RETRY_BLOCK = """
A PREVIOUS attempt at this test file failed to compile or pass. Its content \
was:
{previous_attempt}

It failed with this error:
{previous_error}

Fix ONLY what's needed to resolve this specific error — a wrong method/\
field/enum-constant name, a wrong argument type or count, a type that \
doesn't exist. If the error names a symbol that doesn't exist on some type \
(e.g. "cannot find symbol" / "incompatible types"), that means your \
previous guess at that type's shape was wrong — remove or replace whatever \
depends on it rather than guessing again the same way. Keep everything \
else from the previous attempt that isn't implicated by the error.
"""


def build_coverage_prompt(
    file_path: str,
    file_content: str,
    coverage: float,
    uncovered_lines: int,
    uncovered_conditions: int,
    test_file_path: str,
    existing_test_content: str | None,
    previous_attempt: str | None = None,
    previous_error: str | None = None,
) -> str:
    if existing_test_content is not None:
        test_file_status = "This test file already exists — add to it, don't start over."
        test_file_content = f"EXISTING TEST FILE CONTENT:\n{existing_test_content}"
    else:
        test_file_status = "This test file does not exist yet — create it from scratch."
        test_file_content = "(no existing test file)"

    retry_block = ""
    if previous_attempt is not None and previous_error is not None:
        retry_block = _RETRY_BLOCK.format(previous_attempt=previous_attempt, previous_error=previous_error)

    return COVERAGE_SKELETON.format(
        file_path=file_path,
        coverage=coverage,
        uncovered_lines=uncovered_lines,
        uncovered_conditions=uncovered_conditions,
        file_content=file_content,
        test_file_path=test_file_path,
        test_file_status=test_file_status,
        test_file_content=test_file_content,
        retry_block=retry_block,
    )
