# 花江校区地点数据

`huajiang.json` 来自 © OpenStreetMap contributors，遵循 [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/)。代码的 MIT 许可不覆盖此数据库。

- 获取日期：2026-11-19。
- 来源：`https://api.openstreetmap.org/api/0.6/map?bbox=110.4022,25.3057,110.4209,25.3231`。
- 每条地点保存原始 OSM 对象链接和名称；部分名称沿用旧“信科”标注。
- 坐标系：WGS84。点对象使用节点坐标；建筑轮廓使用去重顶点均值，作为建筑代表点。
- 默认提交时由 `coord-convert` 转为 GCJ-02，与参考桂电客户端的处理保持一致。
- 学校 [2025 年花江校区示意图](https://www.guet.edu.cn/hjxqdt/list.htm) 用于校区布局核对；示意图没有经纬度，未从图中编造坐标。

更新数据库时应保留来源、坐标系和 ODbL 许可说明。
