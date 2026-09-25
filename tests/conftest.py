import os
import tempfile

# 在导入 app 之前指向临时数据库（conftest 先于测试模块被导入）
_tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmpdir, "test.db")
