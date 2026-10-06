"""WingMan · 聊天僚机 —— 本地优先的对话参谋系统。

这个文件的**导入时机**是它存在的第二个理由：它是整个包的最外层，
在任何子模块之前执行。下面那段环境变量必须在 numpy 被导入之前生效，
否则等于没写 —— 所以它只能放在这里，不能挪去 config.py 或 main.py。
"""

from __future__ import annotations

import os

# ------------------------------------------------------------ BLAS 线程上限
#
# 为什么在这里：numpy 一被导入，OpenBLAS 就在 import 期把线程池建好、
# 把每个线程的工作缓冲一起预留掉。之后再设环境变量已经晚了。
# `import app.*` 必先执行这个文件（`import app.desktop` → `app/__init__.py`），
# 所以这里是唯一能保证「早于 numpy」的位置。
#
# 为什么是 1：numpy 在本项目里只干一件事 —— 几千条 512 维向量算余弦相似度
# （见 memory/retriever.py）。这个规模下多线程只会增加调度开销，拿不到任何好处。
# 而 OpenBLAS 默认按**逻辑核数**开线程：本机 24 逻辑核 → 24 个线程 + 各自的
# 工作缓冲。实测（同机、同 venv、只 import numpy）：
#
#     默认                    提交 760.6 MB   线程 27
#     OPENBLAS_NUM_THREADS=1  提交  43.1 MB   线程  4
#
# 这是整个程序里最大的一笔内存，而它换来的并行度我们一点都用不上。
#
# 用 setdefault 而不是硬赋值：用户若自己设过（例如要跑别的批处理），
# 以用户的为准 —— 我们只是提供一个更省内存的默认值。
for _var in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    os.environ.setdefault(_var, "1")
del _var      # 别把这个循环变量留在包命名空间里

__version__ = "0.2.0"
