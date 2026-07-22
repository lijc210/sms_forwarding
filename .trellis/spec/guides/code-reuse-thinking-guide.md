# 代码复用思考指南

> **目的**：在创建新代码之前停下来思考——它是否已经存在？

---

## 问题

**重复代码是不一致 bug 的首要来源。**

当你复制粘贴或重写现有逻辑时：
- Bug 修复不会传播
- 行为随时间产生分歧
- 代码库变得更难理解

---

## 编写新代码之前

### 第 1 步：先搜索

```bash
# 搜索相似的函数名称
grep -r "functionName" .

# 搜索相似的逻辑
grep -r "keyword" .
```

### 第 2 步：问这些问题

| 问题 | 如果是... |
|------|-----------|
| 是否存在相似的函数？ | 使用或扩展它 |
| 这个模式是否在其他地方使用？ | 遵循现有模式 |
| 这能否成为共享工具？ | 在正确的位置创建它 |
| 我是否从另一个文件复制代码？ | **停止** - 提取为共享 |

---

## 常见的重复模式

### 模式 1：复制粘贴函数

**坏**：将验证函数复制到另一个文件

**好**：提取到共享工具库，在需要的地方导入

### 模式 2：相似的组件

**坏**：创建一个与现有组件 80% 相似的新组件

**好**：通过属性/变体扩展现有组件

### 模式 3：重复的常量

**坏**：在多个文件中定义相同的常量

**好**：单一数据源，随处导入

### 模式 4：重复的载荷字段提取

**坏**：多个消费者在本地对相同的 JSON/事件字段进行类型转换：

```typescript
const description = (ev as { description?: string }).description;
const context = (ev as { context?: ContextEntry[] }).context;
```

即使代码只有两行，这也是重复的契约逻辑。每个消费者现在都有自己的有效载荷定义。

**好**：将解码器、类型保护或投影放在数据所有者旁边：

```typescript
if (isThreadEvent(ev)) {
  renderThreadEvent(ev);
}
```

**规则**：如果同一个无类型载荷字段在 2 个以上的地方被读取，在添加第三个读取者之前创建一个共享的类型保护/规范化器/投影。

---

## 何时抽象

**应当抽象的情况**：
- 相同的代码出现 3 次以上
- 逻辑足够复杂，可能包含 bug
- 多个人可能需要它

**不应当抽象的情况**：
- 只使用一次
- 简单的一行代码
- 抽象比重复更复杂

---

## 批量修改之后

当你对多个文件做了相似的修改时：

1. **回顾**：是否所有实例都覆盖了？
2. **搜索**：运行 grep 查找是否有遗漏
3. **考虑**：是否应该抽象？

### Reducer 应使用穷举结构

当状态从类似 action 的值（`action`、`kind`、`status`、`phase`）派生时，优先使用一个 `switch` 的 reducer 而非分散的 `if/else` 更新。

```typescript
// 坏 - 特定 action 的状态转换难以审查
if (action === "opened") { ... }
else if (action === "comment") { ... }
else if (action === "status") { ... }

// 好 - 一个 reducer 拥有转换表
switch (event.action) {
  case "opened":
    ...
    return;
  case "comment":
    ...
    return;
}
```

当事件日志是数据源时，这很重要。reducer 是已记录的回放模型；展示代码和命令不应重复该回放模型的部分内容。

---

## 提交前清单

- [ ] 搜索了现有的相似代码
- [ ] 没有应该被共享的复制粘贴逻辑
- [ ] 共享解码器外没有重复的无类型载荷字段提取
- [ ] 常量在一处定义
- [ ] 相似的模式遵循相同的结构
- [ ] Reducer/action 转换生活在一个 reducer 或命令调度器中

---

## 陷阱：Python if/elif/else 穷举检查

**问题**：Python 的 if/elif/else 链没有编译时的穷举检查。当你向 `Literal` 类型（例如 `Platform`）添加新值时，现有的 if/elif/else 链会静默地落入 `else` 并采用错误的默认值。

**症状**：新平台部分工作——有些方法返回 Claude 的默认值而不是平台特定的值。没有引发错误。

**示例**（`cli_adapter.py`）：
```python
# 坏："gemini" 落入 else，返回 "claude"
@property
def cli_name(self) -> str:
    if self.platform == "opencode":
        return "opencode"
    else:
        return "claude"  # gemini 静默得到 "claude"！

# 好：每个平台都有显式分支
@property
def cli_name(self) -> str:
    if self.platform == "opencode":
        return "opencode"
    elif self.platform == "gemini":
        return "gemini"
    else:
        return "claude"
```

**预防**：当向 Python `Literal` 类型添加新值时，搜索所有在该类型上切换的 if/elif/else 链，并添加显式分支。不要依赖 `else` 对新值的正确性。

---

## 陷阱：产生相同输出的非对称机制

**问题**：当两种不同的机制必须产生相同的文件集时（例如 init 的递归目录复制 vs update 的手动 `files.set()`），结构变化（重命名、移动、添加子目录）只通过自动机制传播。手动机制静默地偏离。

**症状**：Init 工作完美，但 update 在错误路径创建文件或完全遗漏文件。

**预防**：
- **最佳**：消除非对称性——让手动路径调用自动路径（例如 `collectTemplateFiles()` 调用 `getAllScripts()` 而不是维护自己的列表）
- **如果非对称性无法避免**：添加一个比较两种机制输出的回归测试
- 迁移目录结构时，搜索所有引用旧结构的代码路径

**真实案例**：`trellis update` 有一个手动 `files.set()` 列表，包含 11 个脚本，而 `getAllScripts()` 已经跟踪了这些脚本。修复：将手动列表替换为 `for..of getAllScripts()` 循环。参见 v0.4.0-beta.3 中的 `update.ts` 重构。

---

## 模板文件注册（Trellis 特定）

向 `src/templates/trellis/scripts/` 添加新文件时：

**单一注册点**：`src/templates/trellis/index.ts`

1. 添加 `export const xxxScript = readTemplate("scripts/path/file.py");`
2. 添加到 `getAllScripts()` Map

这样就完成了。`commands/update.ts` 直接使用 `getAllScripts()`——无需手动同步。

**为什么这很重要**：如果没有在 `getAllScripts()` 中注册，`trellis update` 不会将文件同步到用户项目。Bug 修复和功能更新将不会传播。

**历史**：在 v0.4.0-beta.3 之前，`update.ts` 有一个自己的手动维护文件列表，经常与 `getAllScripts()` 不同步。这导致 11 个 Python 文件在 `trellis update` 期间被静默跳过。修复：消除重复列表，使用 `getAllScripts()` 作为单一数据源。

### 新脚本快速检查清单

```bash
# 添加新的 .py 文件后，验证它已在 getAllScripts() 中：
grep -l "newFileName" src/templates/trellis/index.ts  # 应匹配
```

### 模板同步约定

`.trellis/scripts/`（自用版）和 `packages/cli/src/templates/trellis/scripts/`（模板版）必须保持一致。编辑 `.trellis/scripts/` 后，始终同步：

```bash
rsync -av --delete --exclude='__pycache__' .trellis/scripts/ packages/cli/src/templates/trellis/scripts/
```

**陷阱**：使用错误的源/目标路径运行 rsync 可能会创建嵌套的垃圾目录（例如 `.trellis/scripts/packages/cli/...`）。运行前始终仔细检查路径。
