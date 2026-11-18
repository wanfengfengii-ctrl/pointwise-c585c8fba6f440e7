# 长响应采集公开契约

## 使用与兼容
试验室需要在有限经典CAN留存中还原长诊断响应，并在查询已结束后仍能核对原归属。README.md描述的V1、全部原文件及原测试继续有效。新增CaptureService.capture_isotp(run_id, frames, channels, timing)，构造器及参数格式、异常类型、报告身份沿用capture；旧capture与decode_single_frame的行为不变，包括其不支持多帧的诊断。新增入口接纳单帧和本文件限定的多帧响应，不要求新增命令行子命令。V1与新入口使用同一run_id命名空间；同入口同输入幂等，跨入口复用run_id必须冲突。新成果固定所用采集口径，report、verify、重开及离线包按原口径工作，旧成果不得被新规则重解释。输入身份仍覆盖全部原帧、次序、配置及目录，并区分采集口径。内部存储表示由实现者决定，不要求改写已有数据。

原始结构和配置错误整次拒绝且保全旧成果。合法原件中的协议、关联及解释错误是报告内容，不让整份采集失败。时间非递减，同刻保持输入顺序；每个原帧（包括未知CAN ID）之前只结清严格早于当前时刻的期限。每个帧处理完后才处理下一帧；等于期限的帧仍可生效。结束时在逻辑期限结清全部活动操作及传输，无真实等待。允许全量离线计算，正文最多4095字节，无新增内存、局部计算、并发或进程恢复要求。

## 有限传输格式与终结
只支持普通寻址经典CAN，单帧规则沿用V1；查询仍是单帧且恰为22加一个两字节DID。每ECU至多一条在途长响应，与诊断操作是否结束分开管理。响应CAN ID承载FF/CF，请求CAN ID承载反向FC。请求侧FF/CF只产生unsupported_pci诊断，不启动或扰动响应传输；其他ECU及未知通道不改变该ECU传输，但全局时间推进仍会结清期限。

FF必须恰有8个CAN数据字节：高半字节1，低半字节与下一字节组成12位总正文长度8至4095，其后6字节是正文前缀。合法新FF终止同ECU旧传输（replaced）再开始新传输；旧传输的归属不转移。无论是否能绑定操作，合法FF均开始传输。首帧后进入等待FC，期限为首帧时间加N_Bs。

FC至少3字节，高半字节3，低半字节为0 CTS、1 WAIT或2 OVERFLOW；第三字节STmin仅接受0，后续字节为填充。仅在等待FC时接纳FC；CTS按第二字节设置块大小，0表示直到完成，进入等待CF且期限为CTS时间加N_Cr。WAIT忽略块大小并将等待期限更新为本帧时间加N_Bs，不设额外次数上限；OVERFLOW终止（overflow）。其他状态收到FC终止（unexpected_fc）；错误长度、状态值或非零STmin终止（malformed）。

CF高半字节2，低半字节序号从1开始并模16递增。必须在等待CF时收到，否则终止（unexpected_cf）；错误序号终止（sequence）。非末CF必须恰有8字节并贡献7字节；末CF须足以提供全部剩余字节，超过剩余长度的字节只作填充。短CF终止（malformed）。非末CF后重启N_Cr；若有限块额度耗尽，则改为等待FC，期限为该CF时间加N_Bs。末CF完成优先于块额度耗尽，不再等FC。

超期分别终止为N_Bs_timeout或N_Cr_timeout，结论时刻是原期限而不是下一帧时间；end_frame为null。其他帧引发的结论使用该帧时间及位置。响应侧SF无论是否有效，先终止在途传输（interrupted），再按V1处理该SF；请求SF不终止响应传输。响应侧坏FF、空数据、未知PCI终止在途传输（malformed），不开始新传输；响应侧FC属于unexpected_fc。没有在途传输的孤立CF/FC或坏传输帧只产生带原帧位置的协议诊断，不能补建或复活传输。失败不产生完整消息或部分正文；已经完成的其他操作和传输保留。

## 首帧归属与业务状态
FF的前三个正文字节只有62加两字节DID才具有绑定资格。在首帧处理时，将它绑定当时同ECU尚未终结且DID匹配的操作；其后不得改绑。其他前缀、无活动操作或DID不符仍完成传输核验，但始终未归属，并保存原因。负响应仍必须恰为三字节7F 22 NRC，因而不存在合法的长0x78或长业务否定。

首帧不延长P2或P2star。完整单帧78仍按V1在接收时延期；正响应必须完整、合法且原绑定操作尚未终结才使其success。传输失败只增加原绑定操作证据，不提前终结它、不延期，也不冒充negative。旧操作超时后收齐的正文保留在旧操作证据中，旧操作保持timeout，后来同DID查询不能获得该正文。未归属传输收齐时也不得按当前操作重新寻找归属。活动操作期间的新查询仍记录关联错误，不替换旧操作。

## 可观察报告
旧单帧消息、操作、字段与诊断格式保持。新入口结果另有transfers数组，按FF位置排序；每条含id（run_id加":response:"加首帧位置）、ecu、start_frame、start_ms、declared_length、end_frame、end_ms、status（complete或failed）、reason（complete为null，失败为上述代码）、operation_id、binding_reason、frames、body、segments。binding_reason在有归属时为null，未归属为response_format、no_active_operation或did_mismatch，按此前缀、活动操作、DID的顺序判断。frames按输入顺序记录FF、已消费FC/CF及导致终止的帧；新FF或SF导致旧传输终止时，该帧也作为旧传输的终止证据。超期不虚构原帧。complete的body为恰好声明长度的字节数组；failed的body为null。

segments只记录实际消费的正文区间，每项为{frame,body_range,can_range}，范围从0计数、左闭右开。body_range针对整个响应正文，can_range针对该原帧的data；FF从CAN下标2起，CF从下标1起。排除PCI、FC及填充；成功时依序恰好覆盖正文，失败时可保留已消费前缀的来源但不能将它作为消息交付。

每个操作增加transfer_ids，按FF位置列出全部原绑定传输，包括失败及迟到者。operations.responses仍只含有业务资格的完整响应；迟到正文及传输失败通过transfer_ids和transfers读取，不进入responses或生成解释字段。完整长消息进入messages，按完成位置与单帧消息混合排序；其message保留ecu、direction、body，frame为FF位置，t_ms为完成时间，另含transfer_id和segments，不使用单帧body_range。长正响应在responses中的kind、did、nrc、data仍按V1分类，其message采用该长消息格式。

成功长响应沿用绑定目录及精确计算。长响应字段的source为{body_range,segments}，body_range仍是字段在完整响应中的范围（含前三字节服务与DID），segments是与该范围相交裁切后的上述来源列表。跨帧字段不得伪造一个连续CAN区间；单帧字段source完全不变。未知DID及长度错误沿用V1，不生成部分字段。report的ECU筛选还须筛选transfers，保留固定身份和完整原件。

原帧、采集口径、传输结论、归属、操作及字段共同固定。verify必须从保存原帧按固定口径重算并比较，包括失败、未归属和迟到传输；只核对摘要不足。修改原帧、传输归属或字段来源后不得通过复核。导出仍使用原接口，自包含重开读取和复核，已有目标拒绝覆盖；无需新增导出格式产品。

## 材料与运行
samples/long_response.json提供两份完整输入和手算期望：timely在期限10收齐，跨FF/CF字段02 03等于515，乘1/10为103/2；late在9开始、旧操作10超时、11新查询、12收齐，两个操作分别在10和21超时，正文仅归旧操作。新入口尚未实现，不能把其缺失视为V1故障。

python3 -B -m tools.long_scenario --check-materials只检查样例和手算材料；不证明功能已经实现。python3 -B -m tools.long_scenario在功能完成后执行真实采集、重开、复核及离线交付，失败须非零退出。它只覆盖主事故和正常对照，不替代其他公开边界回归。

保留并执行全部原测试；补独立小序列对照及流控、块耗尽、序号15到0、超期与同刻次序、替换/失败、完整78和双ECU交错的联合回归。验证结果须包括精确正文、归属、状态、字段来源及重开复核。简述传输与操作的状态交接。.pointwise-runtime.json沿用version=1、kind=cli、setup为argv数组列表及verify/demo各为argv数组；更新verify同时执行全部原回归及新增业务核验，demo展示本文件真实长响应结果。README.md与RUNNING.json原V1运行方式保持可用。
