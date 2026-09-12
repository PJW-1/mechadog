"""Desktop launcher for the local MechDog mode controller."""

import json
import os
import sys
import subprocess
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from uuid import uuid4

from settings import SETTINGS, require_settings

ROOT = SETTINGS.data_dir
CODE_ROOT = Path(__file__).resolve().parent
PYTHON = Path(sys.executable)

MESSAGES = {
    'DOWNLOAD_READY': ('다운로드 모드 확인됨',
                       '설치 준비가 되었습니다. 일반 부팅을 누르면 앱이 실행됩니다.', '#167248'),
    'APPLICATION_RUNNING': ('앱 실행 중',
                            '로봇의 실행 로그를 확인했습니다. 다운로드 모드는 아닙니다.', '#167248'),
    'NORMAL_BOOT_CONFIRMED': ('일반 부팅 확인됨',
                              '새 부팅 로그와 구동 OFF · SAFE ON 상태를 확인했습니다.', '#167248'),
    'AUTO_DOWNLOAD_FAILED': ('자동 다운로드 진입 실패',
                             '이 연결에서는 PC 신호만으로 진입되지 않았습니다.\n'
                             '왼쪽 BOOT를 누른 채 → 오른쪽 EN을 눌렀다 놓기 → 왼쪽 놓기.\n'
                             '그다음 [상태 확인]을 누르세요.', '#995900'),
    'PORT_NOT_FOUND': ('USB 연결을 찾지 못했습니다',
                       'PC USB 연결과 로봇 전원을 확인해 주세요. 설정한 포트와 대상 기체를 확인하세요.', '#995900'),
    'CONNECTION_ERROR': ('연결을 확인할 수 없습니다',
                         '다른 설치 도구나 시리얼 모니터를 닫은 뒤 다시 확인해 주세요.', '#995900'),
    'MODE_UNKNOWN': ('모드 확인 필요',
                     '앱 로그나 다운로드 응답을 확인하지 못했습니다. 연결 후 다시 확인해 주세요.', '#995900'),
    'SAFETY_REFUSED': ('펌웨어 또는 장치 확인 필요',
                       '검증된 구동 OFF 펌웨어 기록과 일치하지 않아 전환을 중단했습니다.', '#a4262c'),
    'BOOT_ERROR': ('부팅 오류 감지',
                   '재부팅 또는 오류 로그가 감지되었습니다. 로그를 확인해 주세요.', '#a4262c'),
    'ACTUATORS_ON': ('구동 ON 펌웨어 감지',
                     'USB 연결 중 구동 금지 조건 때문에 추가 전환을 중단했습니다.', '#a4262c'),
}


class ModeWindow:
    def __init__(self, root):
        self.root = root
        self.process = None
        self.report = None
        root.title('메크독 모드 전환')
        root.geometry('680x505')
        root.minsize(640, 470)
        root.configure(bg='#f4f6f8')
        root.option_add('*Font', ('맑은 고딕', 10))
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TButton', padding=(12, 12), font=('맑은 고딕', 11))
        container = tk.Frame(root, bg='#f4f6f8', padx=26, pady=22)
        container.pack(fill='both', expand=True)
        tk.Label(container, text='메크독 모드 전환', bg='#f4f6f8', fg='#172b40',
                 font=('맑은 고딕', 21, 'bold')).pack(anchor='w')
        tk.Label(container, text=f'PC USB 연결 · {SETTINGS.port}', bg='#f4f6f8', fg='#526071').pack(anchor='w', pady=(5, 17))
        card = tk.Frame(container, bg='white', padx=18, pady=15)
        card.pack(fill='x')
        self.title = tk.Label(card, text='상태를 확인해 주세요', bg='white', fg='#172b40',
                              font=('맑은 고딕', 15, 'bold'), anchor='w')
        self.title.pack(fill='x')
        self.description = tk.Label(card, text='로봇을 연결한 뒤 아래 버튼을 눌러 주세요.',
                                    bg='white', fg='#435065', anchor='nw', justify='left',
                                    wraplength=560, height=4)
        self.description.pack(fill='x', pady=(10, 0))
        self.checked = tk.Label(card, text='아직 확인하지 않음', bg='white', fg='#6a7482', anchor='w')
        self.checked.pack(fill='x')
        row = tk.Frame(container, bg='#f4f6f8')
        row.pack(fill='x', pady=(18, 10))
        self.buttons = []
        for col, (label, action) in enumerate((('상태 확인', 'check'), ('다운로드 모드', 'download'),
                                              ('일반 부팅', 'normal'))):
            row.columnconfigure(col, weight=1)
            button = ttk.Button(row, text=label, command=lambda a=action: self.start(a))
            button.grid(row=0, column=col, sticky='ew', padx=(0 if col == 0 else 8, 0))
            self.buttons.append(button)
        self.progress = ttk.Progressbar(container, mode='indeterminate')
        self.progress.pack(fill='x', pady=(0, 12))
        tk.Label(container, bg='#f4f6f8', fg='#526071', justify='left', anchor='w',
                 text='일반 부팅은 설치된 펌웨어를 실행합니다. 현재 검증 앱은 구동 OFF입니다.\n'
                      '다운로드 모드는 자동 진입을 시도하고 실제 응답으로 확인합니다.\n'
                      '펌웨어 설치 후에는 일반 부팅으로 돌아갈 수 있습니다.',
                 wraplength=600).pack(fill='x')
        bottom = tk.Frame(container, bg='#f4f6f8')
        bottom.pack(side='bottom', fill='x', pady=(10, 0))
        ttk.Button(bottom, text='기록 폴더 열기', command=self.open_logs).pack(side='left')
        root.protocol('WM_DELETE_WINDOW', self.close)

    def open_logs(self):
        (ROOT / 'logs').mkdir(exist_ok=True)
        os.startfile(ROOT / 'logs')

    def start(self, action):
        if self.process:
            return
        if not PYTHON.exists():
            messagebox.showerror('실행 환경 없음', f'Python을 찾지 못했습니다.\n{PYTHON}')
            return
        name = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + action + '-' + uuid4().hex[:8]
        self.report = ROOT / 'logs' / (name + '.json')
        self.report.parent.mkdir(exist_ok=True)
        try:
            self.process = subprocess.Popen(
                [str(PYTHON), str(CODE_ROOT / 'mode_control.py'), action, '--report', str(self.report)],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as exc:
            messagebox.showerror('실행 실패', str(exc))
            return
        for button in self.buttons:
            button.state(['disabled'])
        self.started = time.monotonic()
        self.title.configure(text='확인 중…', fg='#215ea4')
        self.description.configure(text='USB 응답을 기다리고 있습니다. 보통 2~20초 정도 걸립니다.')
        self.checked.configure(text='작업 중에는 다른 설치 도구를 실행하지 마세요.')
        self.progress.start(12)
        self.root.after(100, self.poll)

    def poll(self):
        if self.process.poll() is None:
            if time.monotonic() - self.started < 55:
                self.root.after(100, self.poll)
                return
            self.process.kill()
            self.title.configure(text='응답 시간 초과', fg='#995900')
            self.description.configure(text='작업을 중단했습니다. 현재 모드는 확인되지 않았습니다.')
            self.root.after(100, self.finish_timeout)
            return
        try:
            result = json.loads(self.report.read_text(encoding='utf-8'))
            title, desc, color = MESSAGES.get(result['state'], MESSAGES['MODE_UNKNOWN'])
            self.title.configure(text=title, fg=color)
            self.description.configure(text=desc)
        except (OSError, ValueError, KeyError):
            self.title.configure(text='결과를 읽지 못했습니다', fg='#995900')
            self.description.configure(text='현재 모드는 확인되지 않았습니다. 기록 폴더를 확인해 주세요.')
        self.finish()

    def finish_timeout(self):
        if self.process.poll() is None:
            self.root.after(100, self.finish_timeout)
        else:
            self.finish()

    def finish(self):
        self.process = None
        self.progress.stop()
        self.checked.configure(text='마지막 확인: ' + datetime.now().strftime('%H:%M:%S') + ' · 현재 상태는 이후 변경될 수 있습니다.')
        for button in self.buttons:
            button.state(['!disabled'])

    def close(self):
        if self.process:
            self.description.configure(text='현재 USB 작업이 끝나면 창을 닫을 수 있습니다.')
            return
        self.root.destroy()


if __name__ == '__main__':
    window = tk.Tk()
    ModeWindow(window)
    window.mainloop()
