"""Compile and run actual C++ through the editor on a Windows CI runner."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from mechahub.core.code_workspace import find_compiler
from mechahub.ui.programming_page import ProgrammingPage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler', type=Path)
    args = parser.parse_args()
    if os.name != 'nt' or not os.environ.get('GITHUB_ACTIONS'):
        raise SystemExit('This actual compiler check is only for a fresh Windows GitHub runner.')
    compiler = args.compiler
    if compiler is None:
        for candidate in ('C:/msys64/ucrt64/bin/g++.exe', 'C:/msys64/mingw64/bin/g++.exe', 'C:/mingw64/bin/g++.exe'):
            if Path(candidate).is_file():
                compiler = Path(candidate)
                break
        if compiler is None:
            tool = find_compiler()
            compiler = tool.executable if tool else None
    if compiler is None:
        raise SystemExit('No real g++/clang++ available on this CI runner.')
    root = Path(__file__).resolve().parents[1] / 'build' / 'Cpp smoke Україна'
    app = QApplication.instance() or QApplication([])
    page = ProgrammingPage(workspace_dir=root)
    page.workspace.compiler_executable = str(compiler.resolve())
    page.language.setCurrentText('C++')
    page.editor.setPlainText('#include <iostream>\n#include <string>\nint main() { std::string name; std::getline(std::cin, name); std::cout << "MECHAHUB_CPP_OK " << name << " 42\\n"; return 0; }\n')
    page.run_code()
    started = time.monotonic()
    sent = False
    while page.process is not None and time.monotonic() - started < 45:
        if not sent and page.stdin_input.isEnabled():
            page.stdin_input.setText('Привіт')
            page.send_input()
            sent = True
        QTest.qWait(20)
    if page.process is not None:
        page.stop_code()
        deadline = time.monotonic() + 5
        while page.process is not None and time.monotonic() < deadline:
            QTest.qWait(20)
    output = page.output.toPlainText()
    success = page.process is None and 'MECHAHUB_CPP_OK Привіт 42' in output
    proof = {'compiler': str(compiler), 'workspace': str(root), 'success': success,
             'status': page.status.text(), 'output': output[:4000]}
    (root / 'proof.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(proof, ensure_ascii=True))
    page.request_close()
    page.close()
    app.processEvents()
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
