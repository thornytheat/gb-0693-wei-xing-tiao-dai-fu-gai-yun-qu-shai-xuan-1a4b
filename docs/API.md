# 接口

`POST /api/run`：JSON中的csv文本，省略使用样例；或multipart字段file。最大5MB。

`POST /api/m1`至`POST /api/m7`：独立运行到指定模块，JSON结构同运行接口。

`GET /api/history`：历史批次。`POST /api/review`接收id、revision和decision（通过或退回）。`GET /api/evidence.csv`提供单一证据导出。

结构错误422，版本变化409，文件过大413；错误返回code与message。所有页面可从左侧七个模块入口访问。

`GET /api/runs/<id>`读取指定批次；页面与证据CSV通过run_id查询参数绑定批次，模块导航保留该参数。

JSON请求体必须是对象，数组、标量、null、损坏JSON及错误上传字段返回400，且不运行默认样例。

## 云区复查与修正版本

`POST /api/runs/<id>/recheck`：提交云区复查记录，从指定批次派生修正版本。JSON字段`recheck_csv`（或别名`csv`），或multipart字段`file`。复查CSV表头固定为`scene,cloud_w,cloud_e`，坐标契约与主管线一致（支持跨180拆分、全角数字；云区端点留空表示清除该场景遮罩）。

派生只替换M3场景遮罩后重算M3-M7，M1/M2裁决、已签核隔离区与采购证据原样保留在历史版本。成功后当前指针指向新版本，历史版本可经版本链读取，失败（含结构错误422、冲突409、派生异常500）不切换指针，旧版继续可用。

`GET /api/runs/<id>/versions`：版本链与复查请求台账（含running/succeeded/failed状态、基版与目标版本号）。`GET /api/runs/<id>/versions/<revision>`读取指定版本快照。

版本收口规则：复查记录按“批次+版本号”收口；新版本号=版本链头+1，晚到旧修订返回409 `STALE_REVISION`，不能倒退；同版异值（同场景同版本不同云区）以`RECHECK_CONFLICT`隔离，未知场景以`RECHECK_UNKNOWN`隔离。请求键=哈希(批次,复查内容)，相同请求重试幂等且不新增版本，running中断可续跑、failed重试沿用原目标版本号。

`GET /api/evidence.csv?run_id=<id>&revision=<n>`：按版本导出证据；修正版本证据额外包含“云区复查遮罩对照”“复查隔离记录”“新旧覆盖与采购差额”三个区块，可同时核对新旧覆盖差额。不传revision时导出当前版本。

页面`/recheck?run_id=<id>`提供复查提交、版本台账、遮罩新旧对照、覆盖/采购差额与复查隔离面板。全程离线复算，不连接外部卫星平台。
