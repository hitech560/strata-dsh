# 模块边界约定

## 分层

```
strata.core    共享内核：值对象 / 领域事件 / 端口（Protocol）/ 错误语义
strata.<mod>   业务模块，一个模块一个子包
strata.cli     进程入口，只做参数解析与装配，不写业务规则
```

## 依赖方向

- `strata.core` 不依赖任何业务模块。
- 业务模块只依赖 `strata.core` 与自己，**不直接 import 其他业务模块**；需要协作时通过 `core` 里定义的端口（Protocol）由装配层注入。
- 三方依赖只在业务模块内出现，公共 API 不泄漏三方类型。

## 错误语义

所有业务异常派生自 `strata.errors.StrataError`，带 `context` 只放可序列化的诊断信息，不放敏感数据。
