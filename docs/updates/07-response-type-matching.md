# 第 07 批：请求与回包类型匹配

日期：2026-11-07。状态：已验证、两仓已推送。Core：7b8280b；Mobile：cd9c59d。

- Core 与 Mobile 参考网关的等待项记录预期回包类型。
- 只有请求 ID、目标设备和回包类型全部匹配，才能完成等待；查询结果不再误结束动作请求。
- 错误类型的回包不移除原等待项，正确回执到达后仍能正常完成。
- 不改协议、功能或授权范围，不新增重试。

验证：定向 31 项通过；五阶段联合检查通过，Core 186 项、Mobile 25 项通过；协议 20/20、两仓 36 文件一致。

APK、Linux 全量和真机验收限制沿用前批说明。按本次新指令，本批后再做第08批并停止。

[Core 本批差异](https://github.com/dreaminutes/whale-core/compare/f674524...7b8280b) · [Mobile 本批差异](https://github.com/dreaminutes/whale-mobile/compare/f224e82...cd9c59d) · [返回目录](README.md)
