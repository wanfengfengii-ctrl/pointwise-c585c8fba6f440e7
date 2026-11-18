# 长响应实现交接说明

本文件说明本轮新增内容的实现位置、状态交接与复核入口，供下一维护者接手。
业务契约以 `LONG_RESPONSES.md` 为准，这里只解释代码落点。

## 代码落点

| 关注点 | 位置 |
| --- | --- |
| 新入口 `capture_isotp` | `diagcapture/source/ingest.py` |
| 采集口径进入输入身份 | `diagcapture/source/identity.py`、`evidence.py`（`profile` 为 `v1`/`isotp`；V1 省略该成员，已有摘要不变） |
| FF/FC/CF/块/序号/超期状态机 | `diagcapture/transport/isotp.py` |
| 每帧调度、消息排序、字段来源 | `diagcapture/operations/replay.py` |
| 操作与传输的交接 | `diagcapture/operations/session.py`（`complete_long`、`transfer_ids`） |
| 跨帧字段来源裁切 | `diagcapture/did/interpret.py`、`did/values.py` |
| 固定包校验（双口径） | `diagcapture/archive/codec.py` |
| 报告 ECU 筛选含 `transfers` | `diagcapture/archive/read.py` |
| 真实样例演示 | `diagcapture/__main__.py`（`demo` 同时跑 V1 与长响应） |
| 联合回归 | `tests/test_isotp.py` |
| 单一校验入口 | `tools/verify_all.py` |

## 每帧处理顺序

1. 只结清**严格早于**当前帧时间的操作期限和传输期限（先操作后传输）。
2. 解析通道；未知 CAN ID 不产生消息，但时间推进照常。
3. 按方向/PCI 分派：
   - 响应 FF：校验 8 字节与 12 位长度；合法则先以 `replaced` 结束同 ECU 旧传输，再建新传输并按首帧三字节做**一次性**绑定。
   - 响应 CF：仅等待 CF 时接纳，校验模 16 序号与字节数；末帧完成优先于块额度耗尽。
   - 请求 FC：仅等待 FC 时接纳，CTS/WAIT/OVERFLOW 与长度、状态、STmin 校验。
   - 响应 SF：先以 `interrupted` 结束在途传输，再走 V1 单帧逻辑（含完整 `7F 22 78` 延期）。
   - 请求 SF/FF/CF 与未知 PCI：不影响传输；FF/CF 只留 `unsupported_pci` 证据。
4. 传输结论只留证据：失败时 `body` 为 null，已消费区间仍在 `segments`；不生成消息。

## 传输与操作的状态交接

- 绑定在 FF 时固定，`transfer.operation_id` 与 `operation.transfer_ids` 同刻写入，之后不再改绑。
- 传输完成时若原操作仍在活动集，才追加响应并置 `success`；操作已超时则只留在其 `transfer_ids` 中。
- 传输失败从不终结、不延期操作，也不冒充负响应。
- 操作先超时不会终止传输；传输按自己的 N_Bs/N_Cr 走完并保留原归属。
- 超期结论使用逻辑期限（`start/CF 时间 + N_Bs/N_Cr`）且 `end_frame` 为 null；帧引发的结论使用该帧时间与位置。

## 复核与演示

```sh
python3 -B -m unittest discover -s tests -v     # 全部既有与新增回归
python3 -B -m tools.long_scenario                # 样例真实采集、重开、复核、离线
python3 -B -m tools.verify_all                   # 运行清单 verify 执行的单一入口
python3 -B -m diagcapture demo                   # V1 与长响应演示
```

`verify` 从保存的原帧按固定口径重算整份语义结果（含失败、未归属、迟到传输）并逐项比较，
因此只改摘要或只改结论都无法通过复核。V1 报告不含 `profile` 成员，永远按 V1 重放，不被新规则重解释。
