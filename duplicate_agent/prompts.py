"""Fix-generation prompt for the duplicate-code agent — the duplication
equivalent of sonar/techdebt_agent/prompts.py's build_fix_prompt(). Kept in
this package (not the shared one) since the task shape and output format
(a diff extracting duplicated blocks into a helper) is specific to this
domain, not a Sonar-rule fix."""

DUPLICATE_SKELETON = """\
You are refactoring a Java/Spring Boot file to remove code duplication \
flagged by SonarQube's duplicate-code detector (CPD).

FILE: {file_path}
Reported duplication: {density:.1f}% of lines duplicated, {blocks} duplicate \
block(s) detected in this file.

FULL FILE CONTENT:
{file_content}

FIRST, decide which shape this duplication actually is — the fix is
different for each, and picking the wrong one is the main way this goes
wrong:

SHAPE A — a code fragment (method body, chunk of a method, repeated
validation/mapping/formatting logic) that's identical or near-identical to
ANOTHER PLACE WITHIN THIS SAME FILE, at or above roughly 5-10 lines.
SHAPE B — this file is a POJO/DTO/JPA entity whose fields, getters,
setters, equals/hashCode/toString are individually unremarkable but
structurally similar to another class ENTIRELY (a different file, not
shown to you) — there is nothing repeated within this file to point at;
the "duplication" only exists relative to a sibling class.

REQUIREMENTS:
1. Preserve existing behavior exactly — this is a pure refactor, not a
   behavior change.
2. For SHAPE A: extract the duplicated block into a single private (or
   protected, if a subclass needs it) helper method within THIS SAME FILE,
   parameterized on whatever actually differs between the occurrences, and
   replace every occurrence with a call to it. If the same content ALSO
   happens to appear in a different file you can't see, still do this —
   it reduces this file's own duplicated-line count regardless of whether
   the other file's copy is addressed separately.
3. For SHAPE B: {lombok_instruction}
4. Do not rename, reformat, or restructure anything outside what's needed
   for the fix above, even if you notice other issues.
5. Do not introduce new SonarQube findings: no new unused imports/variables,
   no new magic numbers, no reduced readability from over-parameterizing a
   helper for a difference that doesn't actually vary.
6. If neither shape applies cleanly — the "duplicate" blocks look similar
   but actually differ in ways that matter and can't be safely
   parameterized or annotated away from what's visible in this file —
   output NO_SAFE_FIX: <one-line reason> instead of guessing. If you're
   declining SHAPE B specifically because Lombok isn't available, say so
   explicitly in the reason so a human knows adding the dependency is the
   actual unblock.

OUTPUT FORMAT:
{output_format}
"""

_LOMBOK_AVAILABLE_INSTRUCTION = """\
this project already depends on Lombok (confirmed from its build file), so \
replace the manual boilerplate with the appropriate Lombok annotations -- \
@Getter/@Setter for accessor pairs, @EqualsAndHashCode and @ToString for \
those methods, @NoArgsConstructor/@AllArgsConstructor for constructors, or \
@Data if the whole set applies cleanly to this class. Delete the manual \
methods/code Lombok now generates; keep any field or method that has real, \
non-generated logic (custom validation, a non-trivial constructor body, a \
JPA-required no-args constructor with side effects, etc.) exactly as is. \
This is what actually removes the duplicated source text from Sonar's \
count, without touching the other file at all."""

_LOMBOK_UNAVAILABLE_INSTRUCTION = """\
this project does NOT currently depend on Lombok, and adding a new build \
dependency is outside what a single-file diff can safely do -- output \
NO_SAFE_FIX with a reason that names Lombok adoption (@Getter/@Setter/\
@Data annotations replacing the manual boilerplate) as the standard fix, \
so a human knows exactly what unblocks this rather than treating it as \
unfixable."""


def build_duplicate_prompt(
    file_path: str,
    file_content: str,
    density: float,
    blocks: int,
    lombok_available: bool,
    output_format: str = "unified diff",
) -> str:
    return DUPLICATE_SKELETON.format(
        file_path=file_path,
        density=density,
        blocks=blocks,
        file_content=file_content,
        lombok_instruction=_LOMBOK_AVAILABLE_INSTRUCTION if lombok_available else _LOMBOK_UNAVAILABLE_INSTRUCTION,
        output_format=output_format,
    )
