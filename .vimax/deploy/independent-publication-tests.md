# 独立随机系列快速发布helper交付

本次仅新增`.vimax/deploy/publish-independent-safe.py`、对应离线测试，并兼容扩展`import-natural-dialogue.py`；没有修改应用代码、生成helper、已有V稿件、旧发布manifest或服务器，没有commit。当前产品目录里其他Agent的修改未触碰。

## 完成行为

- 固定series=`独立随机-20261015`。V01–V40及V01-R001等从零重开attempt均强制独立证据；原run family/revision分别保存、parent_run=None，不伪造成补丁父稿。旧演进稿与旧V01–V03导入路径保持兼容。
- 新V01–V03也必须有真实完整150、连续编号、不可变snapshots、准确原run字节SHA、绑定canonical消息hash的两份全文报告、review-index和真实全稿contract校验。显式发布列表只接受新registry中publication_eligible=true的唯一label/run_id；未审/0/半稿保留registry历史，不伪装完整稿导入。paused仍暂停、failed保留原错误、模型未审不冒充完成。
- 索引名称为`独立01-R001 · 风格标签 · 完整双审候选/表达备选/需修订 · 模型状态`。兼容字段human_verdict保留，但publication.review_label使用上述准确文案。selected默认None，所有稿默认publication.selected/user_selected=false，没有默认推荐。仅在用户明确选择、且真实模型/合同通过后，显式--selected才标“用户已选”。UI需优先使用publication.review_label和user_selected（应用组件由root统一处理）。
- prepare本地冻结单tar，包含显式版本列表的全部安全原稿文件/attempts/snapshots/报告、versions.json、公共语料原文/来源/LICENSE，以及自身、旧publisher和importer源码；publication-plan记录这三个源文件SHA和运行语料SHA。排除.runtime、私有配置、SQLite/WAL、缓存、锁与临时文件。
- prestage只上传一个tar。服务端先核验tar SHA、成员路径、普通文件类型、无重名/大小写碰撞、精确文件集合和逐文件SHA，再写全新staging，复核后原子rename；不调用extractall。已存在相同manifest则不再上传，但仍完整重新哈希；不同归档拒绝覆盖。tar保存在最终archive之外，不污染精确文件集合。
- 旧publisher的remote事务源码原样冻结复用：一致性备份、双镜像ID pin、全局锁、running任务拒发、旧行/索引/active保全，以及重开入口后绝不自动回滚DB。候选第一次import hook在一个Docker容器内按用户给的完整label顺序导入两遍，旧事务的第二次hook显式跳过；正式导入一个容器一遍。

## 语料实际安装路径

源文件`outputs/独立随机-20261015/lccc.corpus.json`随包冻结为`corpus/lccc.json`。只读实际验证：10,000段，6,493,257字节，SHA256 `20095beb0b9e8aab3d971ac8af434ae9236b0e0534b2a33bf9fe3c18e8aeb84e`。所有会话内容hash/来源引用均校验通过，不是只确认文件存在。每稿version_record.corpus.file_sha256必须等于此处理产物SHA，corpus_reference.sources也必须与处理产物来源逐项匹配，不能发布时悄悄替换另一份语料。

候选库中实际安装到`candidate/state/dialogue/dialogue-corpus/lccc.json`；正式发布在旧事务停后台并完成备份后，安装到`/opt/vimax/shared/state/dialogue/dialogue-corpus/lccc.json`，容器中即默认`DATA_DIR/dialogue-corpus/lccc.json`。安装通过同文件系统硬链接原子生效、不覆盖已有文件；已有同字节复用、不同字节拒绝。正式发布在入口重开前失败，仅在旧DB/索引恢复成功后回收本事务新建的corpus文件；原有文件不动，入口重开后不执行这个回滚。运行验收另外通过真实`GET /api/dialogue-corpus`验证available/数量/sources，并核对实际安装文件SHA；本次未联网执行。

## 离线验证结果

- 新suite **44项通过**（含继承的旧兼容用例）；原导入/发布安全suite **55项通过**。
- 实际SQLite fixture验证52条原run_json字符串、原索引项和active不变，新独立V01两次导入完全一致；未访问线上52条记录。
- 覆盖旧V01–V03/演进/revision兼容；独立revision父None；双审hash/缺失快照/继承上下文/错误人格计划/空语料拒绝；paused真实合同检查；failed完整稿不改状态；selected必须实际goal_met=true/issues=[]；无selected默认；V01也核对人工与模型状态。
- 覆盖tar路径越界/绝对路径/软链接/重复成员/大小写碰撞/额外文件/文件SHA/tar SHA拒绝，且验证失败前没有staging；相同归档重试与冲突不覆盖；fake SSH实际只调用一次put；同容器两遍保持原列表顺序；语料安装/同稿复用/拒覆盖/仅新文件恢复hook。宿主脚本通过Python3.6语法检查，批量subprocess使用兼容参数。
- `git diff --check -- .vimax/deploy/import-natural-dialogue.py`通过。没有执行真实独立版本prepare、SFTP、Docker、candidate、publish或模型请求。

测试命令：

```powershell
$py = 'E:/剧本生成器/vimax-runtime/.venv/Scripts/python.exe'
$env:PYTHONIOENCODING = 'utf-8'
& $py .vimax/deploy/test-independent-publish-safety.py
& $py .vimax/deploy/test-natural-publish-safety.py
```

## 未来使用顺序（本次未执行）

先由root统一审阅、应用产品改动、测试与提交，并构建目标release的两个镜像。target-release必须显式填写真实已构建新release，previous填现场核定的旧release；不复用旧batch/manifest。版本列表从新系列已冻结的注册表审定，包含需要公开保留的失败attempt，绝不读取旧自然群聊注册表。

```powershell
$target = '<YYYYMMDD-最终7位提交>'
$previous = '<现场核定旧release>'
$versions = '<审定的完整label列表，如V01,V01-R001,V02,...,V40>'
$pubArgs = @('--target-release', $target, '--previous', $previous, '--versions', $versions)
# 默认没有用户已选。仅在用户确实选择且该稿实际通过全部检查时：
# $pubArgs += @('--selected', '<用户明确选择的完整label>')
& $py .vimax/deploy/publish-independent-safe.py prepare @pubArgs
& $py .vimax/deploy/publish-independent-safe.py prestage @pubArgs --host $env:VIMAX_TASK_SSH_HOST --port 11476
& $py .vimax/deploy/publish-independent-safe.py candidate @pubArgs --host $env:VIMAX_TASK_SSH_HOST --port 11476
& $py .vimax/deploy/publish-independent-safe.py publish @pubArgs --host $env:VIMAX_TASK_SSH_HOST --port 11476
```

prestage是服务器归档写入，不是本地dry-run；candidate写隔离服务器候选库；publish才正式切换。上述网络命令均需之后授权执行。密码仅环境`VIMAX_TASK_SSH_PASSWORD`，不写包/日志，SSH拒绝未知host key。prepare完成后冻结同一包和源码；修改后使用新target-release，不覆盖旧包。

40个正式family/不同正文/人物与情节独立性由root的register/finalize核验；本helper检查保存的从零生成证据与合同，不声称仅凭字段就证明语义独立。

## 源文件SHA256

- publish-independent-safe.py：`f73339762852c15d832134494afc09b9dcec58b8c5b0261af6a46bb244749527`
- test-independent-publish-safety.py：`08ed6340fff3a3f8c952599993fad3aa939214d7310b3d1182fa11975ef4d62a`
- import-natural-dialogue.py：`cb3adf2a07905467e24e4d03606e8b0586445b9e26659b4914d5a277fca20480`

## 当前已知限制

- 真实新系列稿尚未全批注册，因此尚未执行真实新稿prepare/离线全批导入；当前证据为真实10,000段corpus只读校验与隔离fixture/fake SSH测试。没有实机传输速度或发布成功结论。
- Python3.6是语法与兼容参数检查，尚未在宿主3.6运行新helper；真正import模块运行于新backend镜像的Python3.12。
- 固定为同一份运行语料产物；已存在不同字节的runtime corpus会拒绝覆盖。需要更新语料时必须另外审查更新/恢复策略，不绕过拒绝。
- --selected仅是明确用户选择的CLI入口，本helper不能替用户作选择；默认None。语义独立、40个正式family去重及审阅真实性由register/finalize与实际全文检查负责。
- 入口重开后的运行错误继续遵守旧边界，不自动回滚DB或删除corpus。失败归档/候选/上传tar保留供核查，不做自动清理。
