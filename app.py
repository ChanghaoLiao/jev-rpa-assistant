from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from jevtool.paths import DATA_ROOT, bootstrap_data, knowledge_file

ROOT = Path(__file__).resolve().parent
bootstrap_data()
os.chdir(DATA_ROOT)  # Relative user resources; Clicknium's SDK lookup is bound separately.

def main():
    parser = argparse.ArgumentParser(description='Jev 多层固定话术客服原型')
    parser.add_argument('--demo',action='store_true',help='离线接线演示：模拟结果，不调用API、不发送')
    parser.add_argument('--text',help='用真实Jev测试问题，不读写店铺界面')
    parser.add_argument('--doctor',action='store_true',help='检查配置/话术库（不读窗口、不发送）')
    parser.add_argument('--runtime-check',action='store_true',help='检查打包依赖能否加载（不读窗口、不发送）')
    parser.add_argument('--locator-runtime-check',action='store_true',help='检查真实SDK能否加载当前录制库（不读窗口、不发送）')
    parser.add_argument('--chat-window-check',action='store_true',help='隔离数据下检查当前聊天、自动回到最新消息和截图；不调用模型或发送')
    parser.add_argument('--ui-check',action='store_true',help='隔离环境下检查 Qt 界面与后台初始化（不读聊天页、不发送）')
    args=parser.parse_args()
    if args.demo:
        from jevtool.demo import run_demo
        print('离线模拟演示 / MOCK-NOT-JEV / 不代表真实准确率 / 不发送')
        print(json.dumps(run_demo().as_dict(),ensure_ascii=False,indent=2))
    elif args.ui_check:
        from jevtool.controller import ui_check
        raise SystemExit(ui_check())
    elif args.chat_window_check:
        if not os.environ.get('JEVTOOL_DATA_DIR'):
            raise ValueError('聊天界面诊断必须显式使用隔离的用户数据目录。')
        import threading
        from PySide6.QtWidgets import QApplication
        from jevtool.config import load_config
        from jevtool.desktop import ClickniumDesktop
        from jevtool.audit import register_secrets, exception_details
        qt = QApplication([])
        try:
            cfg = load_config(); register_secrets(cfg)
            checks = ClickniumDesktop(cfg['desktop'],threading.Event(),allow_unreviewed=True).doctor()
            result = {'ok':checks.get('chat_capture_valid') is True, 'checks':checks,
                      'model_calls':0,'sends':0}
        except Exception as error:
            result = {'ok':False,'error':exception_details(error),'model_calls':0,'sends':0}
        (DATA_ROOT / 'chat-window-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False,indent=2))
        qt.quit()
        raise SystemExit(0 if result['ok'] else 1)
    elif args.locator_runtime_check:
        from jevtool.config import load_config
        from jevtool.clicknium_project import check_locator_runtime
        result = check_locator_runtime(load_config())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        (DATA_ROOT / 'locator-runtime-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        raise SystemExit(0 if result['ok'] else 1)
    elif args.runtime_check:
        from clicknium import clicknium, locator, ui
        import pyperclip
        from jevtool.gui import App
        from jevtool.vision import VisionReader
        assert clicknium is not None and locator is not None and ui is not None
        assert pyperclip is not None and App is not None and VisionReader is not None
        print('打包依赖可加载。')
    elif args.doctor:
        from jevtool.config import load_config
        from jevtool.knowledge import Knowledge
        c=load_config()
        kb=Knowledge.load(knowledge_file(c['knowledge']['replies_csv']),
                          knowledge_file(c['knowledge']['categories_json']))
        print(f'配置有效；话术 {len(kb.replies)} 条；已批准自动发送 {sum(r.approved and not r.manual_only for r in kb.replies.values())} 条。')
        print('Jev Key 已填写：',bool(c['jev']['api_key']))
        print('视觉 Key 已填写：',bool(c['vision']['api_key']))
        print('本命令未测试API有效性或Windows客户端。')
    elif args.text is not None:
        import threading
        from jevtool.config import load_config
        from jevtool.service import Service
        service=Service(load_config(),threading.Event(),lambda _:None)
        try:print(json.dumps(service.text_test(args.text).as_dict(),ensure_ascii=False,indent=2))
        finally:service.close()
    else:
        from jevtool.gui import launch
        launch()

if __name__=='__main__':
    main()
