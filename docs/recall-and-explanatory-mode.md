# 按需检索与解释模式

Codex 在已注册项目的实质开发任务开始前，从当前任务、模块或报错中提取关键词，查询本地索引。
默认最多返回 3 张卡，总 JSON 输出不超过 2400 个字符。卡片包含结论摘要、适用条件、记录状态、
来源路径和代码来源是否变化。需要细节时，再读命中的一两条原记录。这个上限按字符计算，
不等于固定 token 数；未进行模型计费或实际 token 节省测量。

索引为 `state/knowledge.sqlite3`，由 Python 标准库的 SQLite FTS5 实现，不调用外部 AI。
发布后同步发生变化的正式记录；首次建立索引会在本地扫描已接受的 JSON 记录，
不会把整个 Vault 发给模型。启动 Hook 只提供索引状态，不再发送“最后几条通用经验”。
同一任务内复用查询结果；任务改变或出现陌生错误时再查询。

检索优先本项目记录，跨项目仅返回明确标记为可复用的经验。历史结论必须核对适用条件；
来源 hash 变化会提示重新检查当前代码。关键词检索可能遗漏同义表达，零命中时可换关键词。
经验用于帮助选择验证步骤，不能保证消除错误。

## 在聊天中切换

- “对这个项目开启解释模式”：后续记录补充项目目标、当前步骤的作用、执行流程、术语和例子。
- “对这个项目切换简洁模式”：后续记录回到简洁工程记录。
- “查看这个项目当前记录模式”：读取当前设置。

每个项目独立保存设置，默认简洁。切换不批量改写历史笔记，也不增加检索输出上限。
解释模式缺少代码或文档来源时，草稿进入待复核；测试通过本身不证明项目讲解准确。
项目主页新增“项目讲解与阅读路径”链接。讲解页使用最近一次有依据的讲解作为入口，
其他讲解记录按创建顺序列出，便于从整体目标进入各步骤的原因、问题和验证。

## 运维命令（由 Codex 执行）

```powershell
python journal.py recall --project engineering-journal --query 'Windows 沙箱 文件占用'
python journal.py mode --project engineering-journal --set explanatory
python journal.py mode --project engineering-journal --set concise
python journal.py index
```

未增加 Hook 定义，无需因本次升级重新审核 Hook。Skill 和全局策略已更新；
新建项目聊天会加载当前说明。2026-10-10 已在本机已注册项目会话中核验真实
SessionStart、PostToolUse、一次 Stop 补漏及关联发布回执。会话证据保留在本机，
新安装需核对自身的实际触发与发布结果。

实现依据：[SQLite FTS5](https://www.sqlite.org/fts5.html)、
[Codex Skills](https://learn.chatgpt.com/docs/build-skills)、
[Codex Hooks](https://learn.chatgpt.com/docs/hooks)。
