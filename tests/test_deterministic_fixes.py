"""
Each fixer must do two things correctly: apply the exact, obviously-safe
transform when the flagged code matches its expected shape, and decline
(return None -> falls through to the LLM) on anything else — a wrong
"decline" just costs an LLM call, a wrong "apply" ships a silent bug. Every
test below checks one specific shape or one specific decline case.
"""

from sonar_autofix_agent.tools import deterministic_fixes as df


def issue(rule_key: str, start_line: int, end_line: int, key: str = "k1") -> dict:
    return {"rule_key": rule_key, "start_line": start_line, "end_line": end_line, "issue_key": key}


# --- S6204: .collect(Collectors.toList()) -> .toList() ---------------------

def test_s6204_applies_and_strips_now_unused_import():
    src = (
        "import java.util.stream.Collectors;\n"
        "class A {\n"
        "  void f() {\n"
        "    var l = items.stream().collect(Collectors.toList());\n"
        "  }\n"
        "}\n"
    )
    patched, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S6204", 4, 4)])
    assert len(applied) == 1
    assert remaining == []
    assert ".toList()" in patched
    assert "Collectors.toList()" not in patched
    assert "import java.util.stream.Collectors;" not in patched


def test_s6204_keeps_import_when_collectors_used_elsewhere():
    src = (
        "import java.util.stream.Collectors;\n"
        "class A {\n"
        "  void f() { var l = items.stream().collect(Collectors.toList()); }\n"
        "  void g() { var m = items.stream().collect(Collectors.joining()); }\n"
        "}\n"
    )
    patched, applied, _ = df.apply_deterministic_fixes(src, [issue("java:S6204", 3, 3)])
    assert len(applied) == 1
    assert "import java.util.stream.Collectors;" in patched


def test_s6204_only_touches_flagged_line_not_other_occurrences():
    src = (
        "void f() { var a = x.stream().collect(Collectors.toList()); }\n"
        "void g() { var b = y.stream().collect(Collectors.toList()); }\n"
    )
    patched, applied, _ = df.apply_deterministic_fixes(src, [issue("java:S6204", 1, 1)])
    assert len(applied) == 1
    lines = patched.splitlines()
    assert "toList()" in lines[0] and "collect" not in lines[0]
    assert "collect(Collectors.toList())" in lines[1]  # untouched


# --- S125: delete commented-out code ----------------------------------------

def test_s125_deletes_exact_flagged_range():
    src = (
        "class B {\n"
        "  void f() {\n"
        "    // int x = 5;\n"
        "    // doSomething(x);\n"
        "    doRealWork();\n"
        "  }\n"
        "}\n"
    )
    patched, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S125", 3, 4)])
    assert len(applied) == 1
    assert remaining == []
    assert "//" not in patched
    assert "doRealWork();" in patched


def test_s125_declines_on_empty_range():
    src = "class B {\n}\n"
    _, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S125", 5, 5)])
    assert applied == []
    assert len(remaining) == 1


def test_s125_declines_when_flagged_range_is_narrower_than_block_comment():
    """Regression: exact live bug (AuthControllerTest.java). Sonar flagged
    only the 'public static void main' line inside a /* ... */ block, not
    the whole block. Deleting just that line left the println/close-brace
    remnant still commented out -- still an S125 violation, silently
    re-flagged as a brand-new open issue on the next scan. Must decline
    (fall through to the LLM) rather than produce a truncated remnant."""
    src = (
        "class T {\n"
        "    /*\n"
        "    public static void main(String args[]){\n"
        '        System.out.println("hi");\n'
        "    }\n"
        "     */\n"
        "}\n"
    )
    _, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S125", 3, 3)])
    assert applied == []
    assert len(remaining) == 1


def test_s125_applies_when_flagged_range_covers_the_whole_block_comment():
    src = (
        "class T {\n"
        "    /*\n"
        "    public static void main(String args[]){\n"
        '        System.out.println("hi");\n'
        "    }\n"
        "     */\n"
        "}\n"
    )
    patched, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S125", 2, 6)])
    assert len(applied) == 1
    assert remaining == []
    assert "/*" not in patched
    assert "public static void main" not in patched


# --- S6242: AWS DefaultCredentialsProvider ----------------------------------

def test_s6242_inserts_credentials_provider():
    src = "S3Client client = S3Client.builder()\n    .region(Region.US_EAST_1)\n    .build();\n"
    patched, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S6242", 1, 3)])
    assert len(applied) == 1
    assert remaining == []
    assert "DefaultCredentialsProvider.builder().build()" in patched


def test_s6242_adds_missing_import():
    """Regression: the fixer used to insert DefaultCredentialsProvider
    without importing it, breaking a file that compiled fine before —
    observed live in a real run (AwsConfig.java: 'cannot find symbol')."""
    src = (
        "package portal.expenses.config;\n\n"
        "import software.amazon.awssdk.regions.Region;\n"
        "import software.amazon.awssdk.services.s3.S3Client;\n\n"
        "class AwsConfig {\n"
        "  S3Client c() {\n"
        "    return S3Client.builder().region(Region.US_EAST_1).build();\n"
        "  }\n"
        "}\n"
    )
    patched, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S6242", 8, 8)])
    assert len(applied) == 1
    assert "import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;" in patched


def test_s6242_does_not_duplicate_import_if_already_present():
    src = (
        "import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;\n"
        "import software.amazon.awssdk.services.s3.S3Client;\n\n"
        "class C {\n"
        "  S3Client c() { return S3Client.builder().build(); }\n"
        "}\n"
    )
    patched, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S6242", 5, 5)])
    assert len(applied) == 1
    assert patched.count("import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;") == 1


def test_s6242_declines_when_already_present():
    src = (
        "S3Client client = S3Client.builder()\n"
        "    .credentialsProvider(DefaultCredentialsProvider.builder().build())\n"
        "    .build();\n"
    )
    _, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S6242", 1, 3)])
    assert applied == []
    assert len(remaining) == 1


def test_s6242_declines_when_not_s3client_builder_shape():
    src = "SomeOtherClient client = SomeOtherClient.builder().build();\n"
    _, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S6242", 1, 1)])
    assert applied == []
    assert len(remaining) == 1


# --- S2629: logging string concatenation ------------------------------------

def test_s2629_applies_single_concat():
    src = 'class C {\n  void f() {\n    log.debug("error: " + err.getMessage());\n  }\n}\n'
    patched, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S2629", 3, 3)])
    assert len(applied) == 1
    assert remaining == []
    assert 'log.debug("error: {}", err.getMessage());' in patched


def test_s2629_declines_multi_concat():
    src = 'log.debug("a: " + a + " b: " + b);\n'
    _, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S2629", 1, 1)])
    assert applied == []
    assert len(remaining) == 1


def test_s2629_declines_no_string_literal():
    src = "log.debug(a + b);\n"
    _, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S2629", 1, 1)])
    assert applied == []
    assert len(remaining) == 1


# --- S5778: assertThrows lambda body extraction -----------------------------

def test_s5778_hoists_setup_out_of_lambda():
    src = (
        "void test() {\n"
        "  assertThrows(IllegalArgumentException.class, () -> {\n"
        '    BigDecimal amount = new BigDecimal("-5");\n'
        "    service.process(amount);\n"
        "  });\n"
        "}\n"
    )
    patched, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S5778", 2, 5)])
    assert len(applied) == 1
    assert remaining == []
    lines = patched.splitlines()
    # setup hoisted above the assertThrows call
    setup_idx = next(i for i, l in enumerate(lines) if "new BigDecimal" in l)
    call_idx = next(i for i, l in enumerate(lines) if "assertThrows" in l)
    assert setup_idx < call_idx
    # lambda body now contains only the single call
    body_idx = next(i for i, l in enumerate(lines) if "service.process" in l)
    assert body_idx > call_idx
    # result must still be syntactically balanced
    assert patched.count("{") == patched.count("}")


def test_s5778_declines_three_statement_lambda():
    src = (
        "void test() {\n"
        "  assertThrows(RuntimeException.class, () -> {\n"
        "    doA();\n"
        "    doB();\n"
        "    doC();\n"
        "  });\n"
        "}\n"
    )
    _, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S5778", 2, 6)])
    assert applied == []
    assert len(remaining) == 1


# --- S1128: unused import ----------------------------------------------------

def test_s1128_deletes_flagged_import_line():
    src = "import java.util.List;\nimport java.util.ArrayList;\nimport java.util.stream.Stream;\n\nclass A {}\n"
    patched, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S1128", 3, 3)])
    assert len(applied) == 1
    assert remaining == []
    assert "java.util.stream.Stream" not in patched
    assert "java.util.List" in patched and "java.util.ArrayList" in patched


def test_s1128_declines_when_line_is_not_an_import():
    src = "class A {\n  void f() {}\n}\n"
    _, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S1128", 2, 2)])
    assert applied == []
    assert len(remaining) == 1


# --- S2094: empty marker interface suppression ------------------------------

def test_s2094_inserts_suppression():
    src = "package x;\n\npublic interface Marker {\n}\n"
    patched, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S2094", 3, 4)])
    assert len(applied) == 1
    assert remaining == []
    lines = patched.splitlines()
    ann_idx = next(i for i, l in enumerate(lines) if "SuppressWarnings" in l)
    decl_idx = next(i for i, l in enumerate(lines) if "interface Marker" in l)
    assert ann_idx == decl_idx - 1


def test_s2094_declines_when_already_suppressed():
    src = 'package x;\n\n@SuppressWarnings("java:S2094")\npublic interface Marker {\n}\n'
    _, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S2094", 4, 5)])
    assert applied == []
    assert len(remaining) == 1


def test_s2094_declines_when_not_a_type_declaration():
    src = "class A {\n  void marker() {}\n}\n"
    _, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S2094", 2, 2)])
    assert applied == []
    assert len(remaining) == 1


# --- S1481 / S1068: unused local variable / private field ------------------

def test_s1481_deletes_literal_initialized_local():
    src = "void f() {\n  int unused = 5;\n  doWork();\n}\n"
    patched, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S1481", 2, 2)])
    assert len(applied) == 1
    assert remaining == []
    assert "unused" not in patched
    assert "doWork();" in patched


def test_s1481_declines_method_call_initializer():
    src = "void f() {\n  int unused = computeSomething();\n  doWork();\n}\n"
    _, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S1481", 2, 2)])
    assert applied == []
    assert len(remaining) == 1


def test_s1068_deletes_literal_initialized_field():
    src = "class B {\n  private static final int UNUSED = 42;\n  void f() {}\n}\n"
    patched, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S1068", 2, 2)])
    assert len(applied) == 1
    assert remaining == []
    assert "UNUSED" not in patched


def test_s1068_declines_constructor_initializer():
    src = "class C {\n  private List<String> unused = new ArrayList<>();\n}\n"
    _, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S1068", 2, 2)])
    assert applied == []
    assert len(remaining) == 1


def test_unused_decl_declines_bare_reassignment():
    """A bare reassignment (`x = 5;`, no type token) must never match --
    only genuine two-token `TYPE NAME = ...;` declarations should."""
    src = "void f() {\n  x = 5;\n}\n"
    _, applied, remaining = df.apply_deterministic_fixes(src, [issue("java:S1481", 2, 2)])
    assert applied == []
    assert len(remaining) == 1


# --- Composition: mixed mechanical + LLM-bound issues, ordering ------------

def test_apply_deterministic_fixes_handles_mixed_batch_bottom_to_top():
    src = (
        "import java.util.stream.Collectors;\n"
        "class Svc {\n"
        "  void f() {\n"
        "    var l = items.stream().collect(Collectors.toList());\n"
        "  }\n"
        "  void g() {\n"
        "    // old debug code\n"
        "    // System.out.println(x);\n"
        "    doWork();\n"
        "  }\n"
        "}\n"
    )
    issues = [
        # deliberately NOT pre-sorted -- caller (FileFixerStep) always
        # passes patch_tools.issues_for_prompt()'s bottom-to-top order,
        # but this proves an out-of-order S125 (line 7-8) processed before
        # the S6204 (line 4) still produces correct results either way
        # since neither edit's line range overlaps or shifts the other.
        issue("java:S6204", 4, 4, "k1"),
        issue("java:S125", 7, 8, "k2"),
    ]
    patched, applied, remaining = df.apply_deterministic_fixes(src, issues)
    assert {i["issue_key"] for i in applied} == {"k1", "k2"}
    assert remaining == []
    assert "toList()" in patched
    assert "old debug code" not in patched
    assert "import java.util.stream.Collectors;" not in patched


def test_apply_deterministic_fixes_leaves_unknown_rule_for_llm():
    src = 'log.debug("x", getSensitiveValue());\n'
    issues = [issue("java:S2068", 1, 1)]  # hardcoded credentials -- no fixer
    patched, applied, remaining = df.apply_deterministic_fixes(src, issues)
    assert applied == []
    assert remaining == issues
    assert patched == src
