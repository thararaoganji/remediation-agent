"""
Section 6: shared skeleton + per-language addenda.

build_fix_prompt() is called by a tool (tools/patch_tools.py), not injected
via ADK's {key} instruction templating, because the issue list needs
pre-formatting (bottom-to-top ordering, cluster filtering per the 5.2/6.1
resolution) that's easier to do in Python than in a template string.
"""

SHARED_SKELETON = """\
You are fixing static analysis findings from SonarQube in an existing production codebase.

FILE: {file_path}
LANGUAGE: {language}

FULL FILE CONTENT:
{file_content}

ISSUES TO FIX IN THIS FILE (already ordered bottom-to-top; fix all of them
in this single pass, in the order given):
{issue_block}

REQUIREMENTS:
1. Preserve existing behavior exactly unless the issue description explicitly
   requires a behavior change (e.g. a real bug fix for a RELIABILITY issue).
2. Fix ONLY the listed issues. Do not refactor, rename, reformat, or "improve"
   any code outside the flagged ranges, even if you notice other problems.
3. Do not introduce new SonarQube findings: no new unused variables/imports,
   no new magic numbers, no reduced test coverage, no new complexity.
4. If two issues in this file overlap or are nested, resolve the outer/structural
   one first and re-derive the inner one against the resulting code, IN THIS
   SAME RESPONSE. You have the full file text, not stale line offsets — do not
   assume you need a second turn to see the effect of the outer fix.
5. If a listed issue cannot be safely fixed without more context than provided
   (e.g. requires understanding a caller's contract elsewhere in the codebase),
   output NO_SAFE_FIX for that issue with a one-line reason, and do not guess.
6. Output a unified diff (or the adapter's expected patch format) — not the
   full rewritten file — so each change is independently reviewable.

OUTPUT FORMAT:
{output_format}
"""

JAVA_SPRING_ADDENDUM = """\
LANGUAGE-SPECIFIC GUIDANCE (Java / Spring Boot):
- Preserve Spring annotations and bean wiring exactly (@Service, @Repository,
  @Autowired/constructor injection, @Transactional boundaries, @RequestMapping
  and related). If a SECURITY or RELIABILITY fix requires touching a
  @Transactional method, do not change its propagation/isolation semantics
  unless the issue is specifically about that.
- Prefer constructor injection over field injection when fixing issues that
  touch dependency injection, but only if the file isn't already consistently
  using field injection elsewhere; don't mix styles within one file.
- For security issues (hardcoded credentials S2068, SQL injection S3649,
  weak crypto S4426): fix via Spring's standard mechanisms rather than ad hoc
  workarounds.
- After fixing, the file must remain valid for the project's declared Java
  language level — do not use syntax newer than that.

STRICT JAVA REMEDIATION RULES (per-rule guidance from observed checkpoint
failures — these are more specific than the general guidance above and win
on conflict for the rule keys they name):
1. No broad suppressions. The only rules an @SuppressWarnings is allowed for:
   stateless CSRF (java:S4502), JPA param counts (java:S107), or marker
   interfaces (java:S2094).
2. S6242/S1874 (AWS): use DefaultCredentialsProvider.builder().build(). Do
   NOT use .create().
3. S6809/S6813 (self-invocation / @Transactional proxy bypass): fix via
   SETTER injection (@Autowired @Lazy on a setter), NOT field or constructor
   self-injection — a self-referencing constructor/field injection risks a
   BeanCurrentlyInCreationException that fails the whole application
   context, breaking the full build, not just this file.

   MANDATORY at every call site that invokes the self-injected field: guard
   it against being null and fall back to `this`.
     Violation: return self.createExpense(...);
     Fix:       return (self != null ? self : this).createExpense(...);
   The self-referencing field is only ever wired by a real Spring context at
   runtime — Mockito's @InjectMocks has no way to satisfy it, so any existing
   Mockito-only unit test (@ExtendWith(MockitoExtension.class), no Spring
   context) of this class WILL NullPointerException the moment it calls a
   method that goes through the self-proxy if this guard is missing, even
   though nothing in the test itself changed. Confirmed live, twice, on the
   same class in the same project: one fix attempt included this guard and
   the checkpoint passed; a separate attempt at the identical issue omitted
   it and broke the checkpoint's unit tests. This is not a stylistic
   preference — omitting it is the direct cause of a build failure.

   If this class already has a Mockito-only unit test AND its content is
   actually visible to you in this prompt (it usually will not be — only
   THIS file's content is provided), you may additionally add
   `instance.setSelf(instance);` to that test's @BeforeEach/setup as a
   belt-and-suspenders fix — an explicit exception to "fix only the listed
   file," the same carve-out rule 9 makes for S1948. The null-safe fallback
   above is mandatory regardless of whether you do this, since the test
   file usually isn't available to edit correctly from this prompt alone.
4. S6208/S6880 (switch expressions): replace if/else chains with a Java 21+
   switch expression (only if the project's language level supports it — see
   the general guidance above). Always handle null safely:
     return switch (obj) {
         case null -> throw new IllegalArgumentException("Target is null");
         case String s -> "string type";
         default -> "other type";
     };
5. S6877/S6916 (pattern match guards): replace a nested if inside a pattern
   match/case block with a `when` guard instead. Do NOT write nested ifs.
     Violation: case String s -> { if (s.isEmpty()) { ... } }
     Fix:       case String s when s.isEmpty() -> ...
6. S2629 (logging): never evaluate methods or concatenate strings inside
   logger parameters. Pass raw objects as {} placeholders, or guard with an
   explicit if (log.isXEnabled()) block.
     Violation: log.debug("error: " + err.getMessage());
     Fix:       log.debug("error: {}", err);
7. S2187 (disabled tests): restore the test (remove the comment-out), and
   add whatever @MockBean(s) are needed to fix any resulting
   UnsatisfiedDependencyException rather than leaving the test broken.
   A disabled test has never actually been run — do not assume it's only
   missing whatever ONE dependency happens to be visible from a quick read;
   check the ENTIRE constructor signature of the class under test (and, for
   a @WebMvcTest, of every class the test's @Import(...) list pulls in too)
   against what the test provides via @Autowired/@MockBean. Every
   constructor parameter of every one of those classes needs a matching
   bean or an explicit @MockBean — a test that still fails to load its
   ApplicationContext after your fix is worse than leaving it disabled,
   since it now breaks the full build instead of just sitting inert.
8. S5778 (assertThrows): extract any setup/construction (e.g. `new
   BigDecimal(...)`, `LocalDate.now()`) OUTSIDE the assertThrows lambda —
   the lambda must contain exactly one method call.
9. S1948 (serialization): you MAY modify related classes outside the
   flagged file to implement Serializable and add a serialVersionUID — this
   is the one explicit exception to "fix only the listed file."
10. S2068/S6437 (hardcoded credentials): when replacing a hardcoded literal
    with an externalized value (System.getenv/@Value/config), do NOT add a
    hardcoded literal as the fallback for when it's missing — a fallback
    string assigned to a password/secret/token-named variable is ITSELF
    exactly the pattern these rules flag, so it just re-triggers the same
    finding under a different guise (observed live: `String x =
    System.getenv("X"); if (x == null) { x = "someLiteralDefault"; }`
    still gets flagged, because the fallback assignment IS a hardcoded
    credential-shaped literal). Fail fast instead — throw
    IllegalStateException (or the class's existing exception convention) if
    the externalized value is missing, rather than silently substituting a
    fake one.
      Violation: String pw = System.getenv("PW"); if (pw == null) { pw = "default"; }
      Fix:       String pw = System.getenv("PW");
                 if (pw == null) { throw new IllegalStateException("PW must be set"); }

DEFENSIVE REFACTORING (avoid introducing new Medium-severity findings):
before finalizing your diff, check it doesn't introduce any of these:
11. S1128 (unused imports): if your fix removed the last usage of an
    imported class, delete that import. Never introduce a wildcard (`.*`)
    import.
12. S1481/S1068 (unused locals/fields): don't leave an orphaned local
    variable or private field that's no longer read.
13. S1192 (duplicated string literals): don't let the same string literal
    appear 3+ times — extract it to a private static final String constant.
14. S3776 (cognitive complexity): if your fix adds multiple try/catch or
    if/else blocks, extract the inner logic into a private helper method
    instead of nesting it inline.
15. Scope: only modify the reported file — the named exceptions are rule 9
    (S1948, may touch related classes for Serializable) and rule 3 (S6809/
    S6813, may touch this class's own existing Mockito unit test file to
    wire the self-reference). Otherwise do not touch unrelated code even if
    you notice other problems in it.
"""


def build_issue_block(issues: list[dict]) -> str:
    lines = []
    for i in issues:
        lines.append(
            f"  - Rule: {i['rule_key']} — {i['rule_name']}\n"
            f"    Category: {i['category']}\n"
            f"    Severity: {i['severity']}\n"
            f"    Lines: {i['start_line']}-{i['end_line']}\n"
            f"    Message: {i['message']}\n"
            f"    Rule description: {i['rule_description']}"
        )
    return "\n".join(lines)


def build_fix_prompt(
    file_path: str,
    language: str,
    file_content: str,
    issues_bottom_to_top: list[dict],
    language_addendum: str,
    output_format: str = "unified diff",
) -> str:
    """issues_bottom_to_top must already have colliding-cluster issues
    removed by the caller (see tools/patch_tools.classify_and_prepare_batch) —
    this function does no cluster logic itself."""
    prompt = SHARED_SKELETON.format(
        file_path=file_path,
        language=language,
        file_content=file_content,
        issue_block=build_issue_block(issues_bottom_to_top),
        output_format=output_format,
    )
    return prompt + "\n" + language_addendum
