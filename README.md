# 卫星条带覆盖与云区筛选

遥感影像采购数据员使用本工具完成：读取轨道条带清单，筛选能覆盖目标纬带的可用影像。

## 业务

目标经度区间支持跨180度拆分；覆盖只算并集，云掩膜从有效条带扣除，重复条带不增加面积。七个模块：条带接收、坐标契约、云区剔除、覆盖并集、缺口定位、采购候选、交付核验。模块独立API及页面入口见docs/API.md。样例中包含真实常见脏数据，原因码和原始行号在隔离面板及证据文件中可复核。

## 启动与验证

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
docker compose config --quiet
```

打开 http://localhost:18207 。点击“运行样例”，或展开输入区提交CSV。切换七个模块查看计算依据。复核区可通过或退回，同一版本仅允许一次复核。证据CSV包含所有阶段与隔离行，批次内容不因复核变更。

## 云区复查（修正版本）

左侧“08 / 云区复查”提交复查记录（`scene,cloud_w,cloud_e`），从当前批次派生修正版本：现行候选采用最新遮罩并重算M3-M7，已签核批次的原条带、覆盖、缺口与采购证据保留在历史版本。复查按“批次+版本号”收口，晚到旧修订拒绝（不倒退），同版异值隔离；相同请求重试幂等、不新增版本，中断可续跑；切换失败保留旧版。复查页与证据CSV（`?run_id=&revision=`）可同时核对新旧覆盖差额。全程离线，不接外部卫星平台。接口与字段见docs/API.md、docs/DATA-DICTIONARY.md。

SQLite保存在命名卷records；容器重建保留历史。只在确认不再需要数据时执行 `docker compose down -v` 重建。端口可通过.env中的PORT调整。本地也可 `pip install -r requirements.txt` 后 `python -m app.main`。

## 定义与限制

详见docs/DATA-DICTIONARY.md、docs/DEPTH.md。单文件最大5MB/一万行，本工具按确定性文件离线复算，不查询外部业务系统。源码位于app，测试位于tests，样例位于sample_data。
