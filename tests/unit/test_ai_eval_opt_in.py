"""AI-EVAL-1 (issue #1040): the AI generate evaluation suite is opt-in.

The live suite skips at module level unless HERD_AI_EVAL=1, no gate or CI
workflow sets that variable or runs the `ai-eval` target, and the suite never
asserts a pass rate (it is a measurement, not a gate). Static checks only, so
this runs in the repo-root unit suite with no stack.
"""

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_SUITE = REPO_ROOT / "tests" / "ai_eval" / "test_generate_eval.py"
MAKEFILE = REPO_ROOT / "Makefile"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _module_level_skip_guards(tree: ast.Module) -> list[ast.If]:
    guards = []
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if test != "os.environ.get('HERD_AI_EVAL') != '1'":
            continue
        calls = [
            stmt.value
            for stmt in node.body
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
        ]
        for call in calls:
            if ast.unparse(call.func) == "pytest.skip" and any(
                kw.arg == "allow_module_level" and ast.unparse(kw.value) == "True"
                for kw in call.keywords
            ):
                guards.append(node)
    return guards


def test_eval_suite_skips_at_module_level_unless_opted_in():
    tree = ast.parse(EVAL_SUITE.read_text())
    assert len(_module_level_skip_guards(tree)) == 1


def test_eval_suite_never_asserts_a_pass_rate():
    tree = ast.parse(EVAL_SUITE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            text = ast.unparse(node.test)
            assert "passed" not in text and "pass_rate" not in text, text


def test_no_workflow_sets_the_opt_in_or_runs_the_target():
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = workflow.read_text()
        assert "HERD_AI_EVAL" not in text, workflow.name
        assert not re.search(r"\bmake\b[^\n]*\bai-eval\b", text), workflow.name


def test_only_the_ai_eval_recipe_sets_the_opt_in_and_no_target_depends_on_it():
    lines = MAKEFILE.read_text().splitlines()
    setters = [
        i
        for i, line in enumerate(lines)
        if "HERD_AI_EVAL=1" in line and not line.lstrip().startswith("#")
    ]
    assert len(setters) == 1
    # The one setter is the recipe line directly under the `ai-eval:` rule.
    assert lines[setters[0] - 1].startswith("ai-eval:")
    for line in lines:
        if line.startswith(("ai-eval:", ".", "#")):
            continue
        rule = re.match(r"^([A-Za-z0-9_.-]+)\s*:(?!=)(.*)$", line)
        if rule:
            prerequisites = rule.group(2).split("##")[0].split()
            assert "ai-eval" not in prerequisites, line
        assert not re.search(r"\$\((?:MAKE|SUBMAKE)\)[^\n]*\bai-eval\b", line), line
