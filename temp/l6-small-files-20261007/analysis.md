# 10.148.7.45:10017 — L6 小文件分析

分析日期：2026-10-07。目标目录：`/data3/docker/volumes/10.148.7.45_10017/db`。
通过 `ssh blj` 跳转后只读检查 LOG、OPTIONS、CURRENT、MANIFEST 和 SST 文件目录。没有修改数据库配置、触发 compaction 或修改项目源码。

结论：当前小文件积累的主要路径是 **L4+L5 compaction 按 L6 文件边界提前切分 → 小文件在 L5 生成 → trivial move 原样进入 L6 → 底层没有按“小文件数量”自动聚合的机制**。日志与 MANIFEST 支持这一链路；不是仅凭参数推测。

远程 LOG、OPTIONS 都确认 RocksDB 9.3.1，编译日期为 2024-11-14，Git sha 为 0。以下源码解释以本地 rocksdb-9.3.1 为准；无法由 Git sha 验证部署二进制是否带有额外补丁。

**规模与来源**

以日志扫描截至 2026-10-07 15:13 左右的状态统计，小文件定义为小于 1 MiB：

| metadata CF / L6 指标 | 结果 |
|---|---:|
| 活跃文件数 | 1155 |
| 总大小 | 32,416,679,708 B，约 30.19 GiB |
| 小于 1 MiB | 718，约 62.2% |
| 小于 16 MiB | 863 |
| 文件大小中位数 | 452,666 B，约 442 KiB |
| 最小文件 | 3185 B |
| 718 个小文件总大小 | 214,035,328 B，约占 L6 字节数的 0.66% |
| 小文件出生于 L5 | 688，约占小文件的 95.8% |
| 小文件出生于 L6 | 29 |
| 小文件出生于 L4 | 1 |

688 个出生于 L5 的小文件中，613 个所属生成任务的输入/输出记录数完全相等。它们不能用“本次 compaction 过滤大量过期记录，把大文件压成小文件”解释。这里的 613 是文件数，不是独立 job 数。

统计方法：关联 `compaction_started`、`table_file_creation`、`compaction_finished`、`table_file_deletion`，再用 `Moving #... to level-...` 更新 trivial move 后的层级。仅看创建时层级会把大量当前 L6 文件错计为 L5。扫描当时重建出的 1264 个活跃文件与目录中 1264 个 SST 对应；另外读取 MANIFEST 重建，确认 metadata L6 为 1155 个。运行中的高层文件数会继续变化。

| 日末或最后采样时间 | metadata L6 文件数 | 大小 |
|---|---:|---:|
| 09-29 | 292 | 30.47 GiB |
| 09-30 | 344 | 30.79 GiB |
| 10-01 | 401 | 30.32 GiB |
| 10-02 | 461 | 29.35 GiB |
| 10-03 | 528 | 28.57 GiB |
| 10-04 | 625 | 29.12 GiB |
| 10-05 | 875 | 33.38 GiB |
| 10-06 | 1030 | 32.89 GiB |
| 10-07 15:13 | 1155 | 30.19 GiB |

总字节数没有相应增长，增长主要体现在文件数量。default CF 此时只有 6 个 L6 文件、约 40 MiB；主要问题在 metadata CF。

**具体样本：SST #158375**

2026-10-06 12:14:34，JOB 42980 执行 `1@L4 + 24@L5 → L5`：

- 原因：`LevelMaxLevelSize`。
- 输入：1,030,921,430 B、50,529 条记录。
- 输出：1,030,927,467 B、50,529 条记录、29 个 SST。
- `num_subcompactions=1`，`output_compression=NoCompression`。
- 输出文件 #158375 只有 3185 B、1 条记录、0 个 deletion，处于这 29 个输出文件中间。
- 12:14:36.270290，日志明确记录 `Moving #158375 to level-6 3185 bytes`。
- 分析快照时文件仍然活跃。

远程证据位置：`LOG:167122` 是任务开始事件，`LOG:167170` 是文件创建事件，`LOG:167212` 是移入 L6 的记录。它们对应分析时的 LOG；后续日志轮转会改变文件名。

读取 `MANIFEST-000011` 并重放 VersionEdit，在该文件作为 L5 输出提交前重建 L6 文件范围。#158375 的 key 范围与 L6 完全不重叠。它最后一个 key 到下一个输出文件 #158376 的首 key 之间：

1. 进入并跨过 L6 #90521，大小 3,109,364 B。
2. 进入 L6 #157811，大小 134,465,913 B。
3. 一共跨过 3 个文件边界；新计入的 grandparent 字节数为 137,575,277 B，超过 128 MiB / 8 = 16,777,216 B。

因此，即使当前输出只有 3185 B，也满足下面的切分分支。

相同方法核对的另外两个样本：

| 文件 | 文件大小 / 记录数 | 生成 job | 跨过边界数 | 新计入 grandparent 字节数 | 与当时 L6 重叠 |
|---|---:|---:|---:|---:|---|
| #138312 | 10,831 B / 3 | 37904 | 3 | 137,489,452 B | 无 |
| #141587 | 9,076 B / 3 | 38748 | 3 | 137,496,504 B | 无 |
| #158375 | 3185 B / 1 | 42980 | 3 | 137,575,277 B | 无 |

这三个任务均没有记录数减少，且均只有一个 subcompaction。三个文件都很快 trivial move 到 L6。MANIFEST 还显示，这些样本在相同的一段 key 范围内先后积累。

**对应源码与机制**

[CompactionOutputs::ShouldStopBefore](../rocksdb-9.3.1/db/compaction/compaction_outputs.cc) 第 305–329 行的优化，是在输出 key 跳过 grandparent 文件时提前结束当前 SST，以减少未来 compaction 的重叠读取。对于 L4+L5 → L5，grandparent 就是 L6。

核心判断是：

```cpp
const size_t num_skippable_boundaries_crossed =
    being_grandparent_gap_ ? 2 : 3;
if (compaction_->immutable_options()->compaction_style ==
        kCompactionStyleLevel &&
    num_grandparent_boundaries_crossed >=
        num_skippable_boundaries_crossed &&
    grandparent_overlapped_bytes_ - previous_overlapped_bytes >
        compaction_->target_output_file_size() / 8) {
  return true;
}
```

这里检查的是 **新跨入的 grandparent 文件大小**，没有检查 **当前输出文件大小**。因此，128 MiB 是目标文件大小，并不是最小文件大小。

还有一个值得重点验证的细节：样本真正被完整跳过的 #90521 只有约 2.97 MiB，小于 16 MiB；判断中的增量还包含下一条 key 所进入的约 128 MiB 文件，因此仍会触发。对应累加逻辑见同文件第 149–153 行。这与“只按实际能够跳过的文件字节数衡量收益”并不等价。

[Compaction::IsTrivialMove](../rocksdb-9.3.1/db/compaction/compaction.cc) 第 517–595 行：当下一层无重叠、压缩等条件满足时，可以只更新文件的层级，不重写 SST；没有最小 SST 大小限制。样本无 L6 重叠且 L5/L6 都使用 NoCompression，日志也直接确认了这次移动。

[VersionStorageInfo::MaxInputLevel](../rocksdb-9.3.1/db/version_set.cc) 第 3254 行：leveled compaction 的常规按容量选取只覆盖到 `num_levels - 2`，也就是 L5。L6 不会因为文件多或文件小就触发一次聚合。后续重叠写入、手工重写、周期性重写等仍可能处理它们；不存在“L6 永远不会 compact”的结论。

这一策略能避免重写没有更新的 L6 数据，但本实例稀疏 key 范围中的小输出，配合 trivial move 和底层缺少小文件聚合条件，导致文件数量持续上升。

**已核对的其他因素**

`OPTIONS-058548`：`target_file_size_base=134217728`、`target_file_size_multiplier=1`、`compaction_pri=kMinOverlappingRatio`、`level_compaction_dynamic_level_bytes=true`、`max_compaction_bytes=3355443200`、`max_subcompactions=2`、`sst_partitioner_factory=nullptr`、自动 compaction 开启、`ttl=2592000`、`periodic_compaction_seconds=2592000`。

- 目标大小不是小文件产生下限。
- 样本 job 实际只有一个 subcompaction，不能归因于并行子任务各自产生尾部小文件。
- 样本无压缩，不能归因于压缩后体积很小。
- 样本位于输出中间，不能仅归因于整个任务结束时不足一个目标文件的尾部数据。
- #158375 生成任务的 L5 输入 ancestor time 距任务时间约 3.8 小时，远小于 TTL 切分路径要求的 15 天；该样本不是 TTL 文件年龄边界切分。
- 137.6 MB 的 grandparent 增量远低于 3.125 GiB 的 max_compaction_bytes；也不满足常规 50% 起步的文件大小提前切分条件。
- 日志中 compaction reason 未出现 Ttl、PeriodicCompaction 或 BottommostFiles；当前主路径是普通层容量 compaction。
- RocksDB 的 `ttl` 是文件 compaction 年龄配置，不等同于 Redis key 的业务 TTL。
- 本地 10.4.2 与 10.10.1 的相应分支仍保留相同判断。不能直接声称升级这两个版本就会消除问题。

边界说明：LOG 没有直接打印 ShouldStopBefore 的分支编号。以上具体分支结论来自日志、MANIFEST 文件范围与 9.3.1 条件的重建，且已对三个样本交叉验证；未对全部 688 个文件逐条重建分支。MANIFEST 解析用于只读取证，没有逐条重新计算 CRC；统计与 LOG、目录交叉核对。没有读取或导出业务 value，报告和证据不包含原始 key。

**处理方向**

1. 短期回收已有碎片：在合适的低峰窗口，对受影响 CF/key 范围执行真正重写 bottommost 的手工 compaction，关注 IO、写延迟和临时磁盘需求。本地 Kvrocks 的 [Storage::Compact](../../src/storage/storage.cc) 第 891–901 行设置了 `kForceOptimized`；部署版本的命令行为仍需核对。此次没有执行。仅整理一次后，原有生成机制仍可能让小文件重新累积。
2. 优先验证源码修正：针对 skippable-grandparent 分支增加合理的最小输出大小门槛，或只按实际可跳过的 grandparent 字节数计算收益。以这三个样本的“间隙内 1–3 条记录 + 跳过小 L6 文件 + 进入大 L6 文件”为回归场景，比较小文件增长、写放大及吞吐。门槛不能直接当作所有切分条件的统一限制，否则可能破坏其他约束。
3. 如希望自动整理，需设计能将相邻小文件一并重写的策略；单纯对一个小文件重新 compact，不一定减少文件数。把 periodic_compaction_seconds 调小也不保证相邻小文件会合并。
4. 调小 max_subcompactions、仅增大 target_file_size_base，或直接升级到上述本地版本，都缺少能消除这条主路径的证据，不宜作为首选修复。

完整计数、选定 OPTIONS、三个任务的事件数据、日志位置及边界比较结果见 [evidence.json](evidence.json)。

