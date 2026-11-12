"""Structural tests for the unibot package (the split of the former studip_bot.py).

They guard the rules that keep the module split correct:
  * layering: a module imports only *earlier* modules at the top (later ones
    only inside functions), so the import graph has no cycles
  * every module can be imported on its own, first, in a fresh interpreter
  * no module references a global name it neither defines nor imports
  * shared mutable state is only reached as state.<name>
  * every top-level name is defined in exactly one module
  * the full Telegram handler table can be built

Run from the project root:
    .venv/bin/python -m unittest discover -s tests -v
"""

import ast
import builtins
import os
import subprocess
import symtable
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["BOT_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "studip_bot_tests.log")
# Tests must never touch the real bot database
from tests.testenv import use_temp_db  # noqa: E402
use_temp_db()

PKG_DIR = os.path.join(ROOT, "unibot")
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}


def _module_order():
    with open(os.path.join(PKG_DIR, "__init__.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and node.targets[0].id == "MODULE_ORDER":
            return [elt.value for elt in node.value.elts]
    raise AssertionError("MODULE_ORDER not found")


ORDER = _module_order()
RANK = {m: i for i, m in enumerate(ORDER)}


def _source(name):
    with open(os.path.join(PKG_DIR, f"{name}.py"), encoding="utf-8") as fh:
        return fh.read()


def _unibot_targets(node):
    """Package modules an import statement pulls in."""
    if isinstance(node, ast.ImportFrom) and node.module:
        if node.module == "unibot":
            return [a.name for a in node.names]
        if node.module.startswith("unibot."):
            return [node.module.split(".", 1)[1]]
    if isinstance(node, ast.Import):
        return [a.name.split(".", 1)[1] for a in node.names if a.name.startswith("unibot.")]
    return []


class PackageLayoutTests(unittest.TestCase):
    def test_every_module_is_listed(self):
        files = {f[:-3] for f in os.listdir(PKG_DIR) if f.endswith(".py") and f != "__init__.py"}
        self.assertEqual(files, set(ORDER))

    def test_top_level_imports_follow_layering(self):
        for mod in ORDER:
            tree = ast.parse(_source(mod))
            for node in tree.body:
                for target in _unibot_targets(node):
                    self.assertIn(target, RANK, f"{mod} imports unknown module {target}")
                    self.assertLess(
                        RANK[target], RANK[mod],
                        f"{mod} imports later module {target} at the top (move it into the function)",
                    )

    def test_function_level_imports_reference_declared_modules(self):
        # Lazy imports also fetch fresh runtime hooks from earlier modules.
        for mod in ORDER:
            tree = ast.parse(_source(mod))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in fn.body:
                    for target in _unibot_targets(node):
                        self.assertIn(target, RANK, f"{mod}.{fn.name}: unknown package module {target}")
                        self.assertNotEqual(target, mod, f"{mod}.{fn.name}: redundant self import")

    def test_state_is_never_imported_by_name(self):
        state_names = {
            t.id for node in ast.parse(_source("state")).body
            for t in (node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)])
            if isinstance(t, ast.Name)
        }
        self.assertTrue(state_names)
        for mod in ORDER:
            tree = ast.parse(_source(mod))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "unibot.state":
                    self.fail(f"{mod}: `from unibot.state import ...` freezes the value; use state.<name>")
                if mod != "state" and isinstance(node, ast.Name) and node.id in state_names and isinstance(node.ctx, ast.Store):
                    self.fail(f"{mod}:{node.lineno}: assigns {node.id} locally instead of state.{node.id}")

    def test_each_name_defined_once(self):
        seen = {}
        for mod in ORDER:
            local_names = set()
            for node in ast.parse(_source(mod)).body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    self.assertNotIn(node.name, local_names, f"{mod}.{node.name} defined twice")
                    local_names.add(node.name)
                    seen[f"{mod}.{node.name}"] = mod
        self.assertGreater(len(seen), 460)

    def test_no_undefined_global_names(self):
        for mod in ORDER:
            src = _source(mod)
            tree = ast.parse(src)
            defined = set(BUILTINS)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)) and node in tree.body:
                    for a in node.names:
                        defined.add(a.asname or a.name.split(".")[0])
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    defined.add(node.name)
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store) and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        defined.add(sub.id)
                    if isinstance(node, ast.Try) and isinstance(sub, (ast.Import, ast.ImportFrom)):
                        for a in sub.names:
                            defined.add(a.asname or a.name.split(".")[0])

            missing = set()

            def walk(table, defined=defined, missing=missing):
                for sym in table.get_symbols():
                    if sym.is_global() and sym.is_referenced() and sym.get_name() not in defined:
                        missing.add(sym.get_name())
                for child in table.get_children():
                    walk(child, defined, missing)

            top = symtable.symtable(src, f"{mod}.py", "exec")
            for sym in top.get_symbols():
                if sym.is_referenced() and sym.get_name() not in defined:
                    missing.add(sym.get_name())
            for child in top.get_children():
                walk(child)
            self.assertEqual(missing, set(), f"undefined names in unibot.{mod}")


class ImportTests(unittest.TestCase):
    def _run(self, code):
        env = dict(os.environ, BOT_LOG_FILE=os.environ["BOT_LOG_FILE"])
        return subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )

    def test_each_module_imports_first_in_a_fresh_interpreter(self):
        # Importing a module first is the hardest case for circular imports:
        # everything it depends on has to load around it.
        code = (
            "import importlib, sys\n"
            f"order = {ORDER!r}\n"
            "failed = []\n"
            "for name in order:\n"
            "    for key in [k for k in sys.modules if k == 'unibot' or k.startswith('unibot.')]:\n"
            "        del sys.modules[key]\n"
            "    try:\n"
            "        importlib.import_module(f'unibot.{name}')\n"
            "    except Exception as exc:\n"
            "        failed.append(f'{name}: {exc!r}')\n"
            "print('FAILED' if failed else 'OK', failed)\n"
        )
        result = self._run(code)
        self.assertIn("OK", result.stdout, result.stdout + result.stderr)

    def test_reverse_import_order(self):
        code = (
            "import importlib\n"
            f"for name in reversed({ORDER!r}):\n"
            "    importlib.import_module(f'unibot.{name}')\n"
            "print('OK')\n"
        )
        result = self._run(code)
        self.assertEqual(result.stdout.strip(), "OK", result.stderr)

    def test_entry_point_exposes_run(self):
        result = self._run("import studip_bot, unibot.app; print(studip_bot.run is unibot.app.run)")
        self.assertEqual(result.stdout.strip(), "True", result.stderr)


class HandlerTableTests(unittest.TestCase):
    """Builds the real Application offline (no login, no network)."""

    # number of app.add_handler(...) calls in the original main()
    EXPECTED_HANDLERS = 130

    @classmethod
    def setUpClass(cls):
        cwd = os.getcwd()
        from telegram.ext import ApplicationBuilder
        from unibot import app as app_module
        os.chdir(cwd)
        cls.application = ApplicationBuilder().token("123456:TEST-TOKEN").build()
        app_module.register_handlers(cls.application)

    def _all(self):
        return [(g, h) for g, hs in self.application.handlers.items() for h in hs]

    def test_handler_count(self):
        self.assertEqual(len(self._all()), self.EXPECTED_HANDLERS)

    def test_authorization_gate_runs_first(self):
        from unibot.auth import _authorization_gate
        first_group = min(self.application.handlers)
        self.assertEqual(first_group, -1)
        self.assertIs(self.application.handlers[-1][0].callback, _authorization_gate)

    def test_callbacks_are_package_coroutines(self):
        import inspect
        for _, handler in self._all():
            cb = handler.callback
            self.assertTrue(inspect.iscoroutinefunction(cb), cb)
            self.assertTrue(cb.__module__.startswith("unibot."), cb.__module__)

    def test_commands_are_unique(self):
        from telegram.ext import CommandHandler
        commands = []
        for _, h in self._all():
            if isinstance(h, CommandHandler):
                commands.extend(h.commands)
        self.assertEqual(len(commands), len(set(commands)))
        for expected in ("start", "status", "login", "logout", "studyplan", "healthcheck"):
            self.assertIn(expected, commands)


if __name__ == "__main__":
    unittest.main()
