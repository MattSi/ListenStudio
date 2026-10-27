"""Check the UI translations. Run: python check_i18n.py

Reports
  1. tr("...") texts with no English entry in i18n_en.py
  2. Chinese strings in the code that are not inside tr() (maybe forgotten)
  3. English entries no longer used by any tr() call
Exit code 1 if 1. or 2. found anything.
"""
import ast
import glob
import os
import re
import sys

from i18n_en import EN

HERE = os.path.dirname(os.path.abspath(__file__))
CJK = re.compile(r"[　-〿一-鿿＀-￯]")
# Strings that are allowed to stay Chinese: code run by another Python, values sent back
# from it (translated where they are shown), language names, and keys for tr(phase).
ALLOWED = {"FW_LOADER", "TRANSCRIBE_HELPER", "WHISPER_HELPER", "LANGUAGES"}
ALLOWED_TEXT = {"转录中", "下载模型", "用 openai-whisper + GPU"}
SKIP_FILES = {"i18n_en.py", "check_i18n.py"}


def check_file(path, used, missing, loose):
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    inside_tr, allowed_nodes = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "tr" and node.args:
            for sub in ast.walk(node.args[0]):
                inside_tr.add(id(sub))
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                used.add(arg.value)
                if arg.value not in EN:
                    missing.append(f"{os.path.basename(path)}:{node.lineno}: {arg.value!r}")
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) in ALLOWED for t in node.targets):
            for sub in ast.walk(node.value):
                allowed_nodes.add(id(sub))
        # Docstrings
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) \
                and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(getattr(node.body[0], "value", None), ast.Constant):
            allowed_nodes.add(id(node.body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and CJK.search(node.value) \
                and id(node) not in inside_tr and id(node) not in allowed_nodes \
                and node.value not in ALLOWED_TEXT:
            loose.append(f"{os.path.basename(path)}:{node.lineno}: {node.value[:60]!r}")


def main():
    used, missing, loose = set(), [], []
    for path in sorted(glob.glob(os.path.join(HERE, "*.py"))):
        if os.path.basename(path) not in SKIP_FILES:
            check_file(path, used, missing, loose)
    used |= {"转录中", "下载模型"}   # looked up with tr(phase)
    unused = sorted(k for k in EN if k not in used)
    for title, rows in (("No English translation", missing), ("Chinese text not in tr()", loose),
                        ("Unused English entries", unused)):
        print(f"{title}: {len(rows)}")
        for r in rows:
            print("   ", r)
    return 1 if missing or loose else 0


if __name__ == "__main__":
    sys.exit(main())
