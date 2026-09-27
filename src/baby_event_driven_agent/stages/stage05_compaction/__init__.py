"""Stage 5（压缩策略）：目录先行，实现共享。

05 与 04 共用一份实现（agent / session / tools / transport 都在
stage04_trajectory），本章的增量（水位自动触发、cap+blob、滚动折叠、
分页工具）落地时直接进本包；在此之前，本包只有测试，
import 指向共享代码，落地后原地翻转到本包。
"""
