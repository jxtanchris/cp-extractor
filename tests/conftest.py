"""
PyTest 全局测试配置与数据库隔离
确保自动化测试在独立的临时测试数据库中运行，绝对不污染正式运行库 data/app.db。
"""

import os
import shutil
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
TEST_DB_DIR = ROOT_DIR / "data" / ".test_runtime"
TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
TEST_DB_PATH = TEST_DB_DIR / "test_app.db"

# 立即设置环境变量（必须在导入 app 之前执行）
os.environ["CP_APP_DB_PATH"] = str(TEST_DB_PATH)


def pytest_sessionfinish(session, exitstatus):
    """测试全部执行完毕后，安全清理测试专用数据库及测试临时沙箱"""
    if TEST_DB_DIR.exists():
        shutil.rmtree(TEST_DB_DIR, ignore_errors=True)
    
    # 清理 output/users 下非正式用户目录（保留正式管理员 8 与工程师 tanjx 129）
    users_sandbox = ROOT_DIR / "output" / "users"
    if users_sandbox.exists():
        for d in users_sandbox.iterdir():
            if d.is_dir() and d.name not in ("8", "129"):
                shutil.rmtree(d, ignore_errors=True)
