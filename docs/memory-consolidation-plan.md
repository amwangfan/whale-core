# Owner 聊天记忆首批实施计划（接口冻结草案）

日期：2026-11-12。规划：Astra；实现：Kimi / Qwen max；协调代理负责审查与上线。

状态：**v1接口与分工冻结，可立即实施**。已结合 Kimi 外部调研与协调者转达的 Qwen 本地结论；MaiBot/AstrBot研究可补评，不阻塞实施或扩大范围。明确不采用“owner正则为主+每日静默配额+所有pending逐次确认”：明确有quote依据的user asserted可自动accepted，模型推断不入。基线由协调者报告为 `feat/memory-consolidation-20261112` / `cc86d89`，本规划未独立核验提交。本文只规划，不授权运行生产迁移、读取用户库、上线或修改设备软件。

## 1. 首批成功定义与非目标

在**可信 owner 私聊和眼镜同 owner 授权空间**里，普通聊天中的明确用户自述，自动成为带证据的事实、偏好或经历；历史窗口之外及同主体新会话仍可检索；用户更正使旧现状退出召回；遗忘后迟到任务、重试、重启不会恢复旧记忆；上下文有硬预算。

不是把 LLM 推断当真相：`accepted` 表示“有证据支持的用户自述”，不是现实世界经验证事实，不等于用户显式确认、永久有效或可以任意分享。

不做群提炼重构、全局旧 API/命令重构、图数据库、新服务、全库历史回填、常驻画像块、自动人格修改、睡眠整理任务、定时主动消息、APK/固件。首批不提供任意历史时点问答；保留时间字段以便以后扩展。

闭环示例：用户“我养的猫叫年糕”→证据+accepted→100轮干扰/重启→同owner眼镜问“我的猫叫什么”正确召回→“名字说错了，它叫团子”明确更正→只按新版现状回答→显式遗忘→在途提炼提交/任务重试均不能恢复。

## 2. 调研取舍与批判

外部研究以本节列出的公开源码与技术文档为依据；实现与验收结果另见 [使用说明](owner-fact-memory.md)。以下为设计取舍，不将上游平台指标视作本项目实测结果。

- 借 [Mem0 提炼纪律](https://raw.githubusercontent.com/mem0ai/mem0/main/mem0/configs/prompts.py)：自足事实、具体名称数字、ObservationDate、No Echo。不照搬固定15–80英文词长度到中文；不允许 assistant attribution 自动晋升。平台 V3 的 additive 行为不能泛化为全部开源版本；只借“不让模型直接删改库”的边界。
- 借 [Graphiti 冲突纪律](https://raw.githubusercontent.com/getzep/graphiti/main/graphiti_core/prompts/dedupe_edges.py) 与 [时间/来源机制](https://raw.githubusercontent.com/getzep/graphiti/main/graphiti_core/utils/maintenance/edge_operations.py)：观察时间和生效时间分离、明确矛盾才失效、来源可回溯。不搬图遍历、多次模型维护管线或数据库。
- 借 [Letta 预算核算](https://raw.githubusercontent.com/letta-ai/letta/archive/letta/schemas/memory.py)：硬上限及分段计数；不加永久profile block、不开放agent自编辑记忆。
- 不采纳“Whale底座已强于Mem0”“只换prompt/加两个字段就够”的结论：现有成员提炼的注释consent硬门与实际默认采集不一致；规则未可靠排除问句/引用；消息级幂等会压掉同消息后续候选；来源ID不等于经过span校验的证据；旧更正/删除未形成异步防复活闭环。
- 不把既有性能bench作为质量验收，也不拿平台LOCOMO分数承诺本地效果。规则兜底同样必须经过证据校验；不能因“模型失败”而自动放宽门槛。

## 3. 本地复用与必须避免的入口

[companion](<../src/whale_core/core/companion.py#L126-L145>)已有 `user_text` 与增强输入区分；自动提炼只能用可信入站原文，不能抽取[用户+助手混合episode](<../src/whale_core/core/companion.py#L451-L462>)。保留现有聊天历史，不将它当无限提炼输入。

[群提炼](<../src/whale_core/memory/extraction.py>)在 [QQ宿主](<../examples/qq_live_workflow.py#L2065-L2099>)和[监听器](<../plugins/weq_channel/wq_push.py#L403-L423>)已有接线，首批不动。

复用 [ResolvedContext](<../src/whale_core/life/conversation_context.py#L159-L187>)、[OwnerRegistry](<../src/whale_core/life/identity.py>)、授权space及pause/opt-out检查。`context=None`、客户端自称owner、相同昵称、相同conversation_id或眼镜前缀本身都不是授权依据。不要借本功能扩大 protected_original 可读规则。

[SQLite事务](<../src/whale_core/memory/sqlite_store.py#L141-L186>)已有 `get/put/get_record/put_record`，tx.delete当前仅删KV。**冻结存储选择：新事实用现有MemoryRecord+独立tag/namespace，任务/水位用现有KV，不新增SQL schema**。删除在事务内put_record覆盖为无summary、无quote、无value的墓碑（status=deleted），不只打删除标签保留正文；新retriever必须排除墓碑。不需要扩展tx.delete_record。事务中只调用tx方法，内存/SQLite做对等测试；不得调用会独立commit的store.put/delete。

## 4. 冻结v1领域契约

以下为接口语义冻结；Python具体文件名可在开工前两人确认，字段不可各自另造一套。领域模块只依赖memory/storage，不反向依赖渠道/companion。

### 4.1 可信入口

`observe(evidence: UserEvidence, authorization: MemoryAuthorization) -> ObserveResult`

UserEvidence：
- `evidence_id`：宿主生成稳定ID；有平台消息ID则按渠道+账号域+消息ID命名；没有则在本轮入口生成server UUID并随主/重模型/补发复用。不要hash原文代替事件ID，不从文本推断身份。
- `owner_id, principal_id, conversation_id, channel, space_id`：由宿主可信解析器填。
- `role='user', text, received_at(UTC), timezone(optional), source_message_id(optional), schema_version=1`。
- `policy_version, generation` 在服务端受理时读取，不接受模型填写。

MemoryAuthorization：服务端可信授权句柄/解析结果，包含主体、可读空间与唯一目标可写空间、策略版本；后台提交必须通过注入的 `authorize_now(owner_id, space_id)` 重新检查当前绑定、录制开关和退出状态，不能只信入队快照。首批仅owner私聊，且该空间明确允许写入。调用者不能把任意字符串space包装成授权。

ObserveResult：`queued | duplicate | skipped_policy | skipped_sensitive | skipped_capacity | failed_storage`，返回evidence_id与不含正文的诊断码。仅可靠入队才报告queued。模型调用不在observe中。

### 4.2 候选提炼器

`extract(evidence, bounded_existing_facts) -> tuple[FactCandidate, ...]`

FactCandidate：`kind=fact|preference|episode, summary, source_evidence_id, quote, span_start, span_end, predicate(optional), value(optional), valid_from(optional), valid_to(optional), assertion=self_report|uncertain|quoted|hypothetical, correction_target(optional)`。

硬约束：只接受当前user证据；offset用Python Unicode codepoint；`text[start:end] == quote`、边界有效、非空且source ID确实存在。quote只能证明出处，不能单独证明summary蕴含；校验还必须检查主体、自述/否定/疑问/假设/引语、具体实体数值一致性。拿不准不accepted。结构化输出失败丢弃/有限重试，不能把自由文本兜底写库。

模型不得输出有效的owner/space/consent/删除命令。`correction_target`只能引用服务端本次提供的同主体同空间候选ID，由服务层检查版本和明确冲突依据。新近≠冲突；“今天去北京出差”不能使“住上海”失效。第三方话语不记入owner档案；助手复述不新造证据。

首批每证据最多4候选，输入原文最多4000字符（超限先拒绝自动提炼并计数，不把截断后语义当完整事实），每summary最多300字符；最多提供8条同域旧事实供比较，禁止全库塞prompt。无用户时区时不得擅自把“明天”折算确定日期；时间歧义留待确认或只保存非现状经历。

### 4.3 写入与生命周期

`commit(job_id, candidates, expected_generation) -> CommitReport`

`correct(fact_id, expected_revision, evidence, replacement) -> MutationReport`

`forget(fact_ids, authorization, expected_revisions) -> MutationReport`

FactRecord：`fact_id, owner_id, space_id, kind, summary, source_refs[{evidence_id, span_start, span_end}], assertion='user_asserted', status=accepted|pending|superseded|expired|deleted, revision, created_at, observed_at, valid_from?, valid_to?, expires_at?, supersedes?, superseded_by?, schema_version`。不写伪造的consent=True或last_confirmed；采集政策和显式确认另记。删除用无内容tombstone，不保留被删summary。

幂等唯一项包含evidence_id+source span+规范化候选身份；同消息多个事实可分别写入。提炼版本用于审计，不能因版本升级绕开已处理证据/删除墓碑。

commit在同事务里复查job lease、generation、当前授权、pause/optout、证据存在/未撤销和事实revision；写新事实/旧事实失效/来源索引/任务完成一起提交。模型只能建议，事务服务唯一裁决。原子失败应保持旧版本完整可用。

更正只有明确同一事实矛盾才supersede，记录双向版本链；歧义不覆盖旧事实，可pending但不召回。自然语言更正必须进入普通聊天同一提炼路径并有集成验收。遗忘首批支持确定的显式命令/API路径，模糊“忘掉那个”必须澄清，禁止相似度top1盲删。

时间：`observed_at`来自事件时间；`created_at`为系统写入；`valid_*`是用户陈述的现实有效区间；`expires_at`是保留/现状使用截止。已有过去经历可作为过去发生的事检索，不能当当前状态。未知有效期为null，不能捏造日期；短期计划只有明确时间才自动成为带截止的现状事实。所有时间解析使用aware UTC；非法时间不accepted。

## 5. 异步方案：需要小型持久队列，不需要新服务

结论：首批要求重启可靠与遗忘防复活，纯daemon内存队列不够。复用本地SQLite/KV事务，做一个窄的持久job ledger；不部署消息系统，不依赖定时主动任务。

- observe在主回答模型调用前完成快速持久受理；不以助手回复成功/非空为前提。主链路不等待提炼LLM。
- 单宿主一个worker，默认并发1；job `pending/leased/completed/failed/cancelled`，lease有过期恢复；重试最多2次。宿主默认模型超时20秒，租约至少60秒且大于模型超时；同owner+space按受理顺序串行领取。该默认值结合合成数据真实模型小样本验证修订，不代表所有模型的延迟保证。后台任务只提炼，不发送消息。
  - **2026-11-12 修订（实测依据 docs/memory-real-model-eval-20261112.md）**：真实模型 20 case 实测 avg 7.87s / max 13.01s，10s 缺省会把 13s 的正常候选当超时丢弃重试。host 装配缺省改为 `WHALE_OWNER_FACT_MODEL_TIMEOUT_S`（缺省 **20s**），`lease_seconds = max(60, model_timeout+30)`；模型调用为**组合单门**（extract 门 busy OR 模型内层门 busy，core/owner_fact_host.CompositeCallGate）——内层超时后底层调用收尾期间 worker 不领取新 job，防止快速重试耗尽误丢非故障证据。20s 评测不宣称 10s 管道同样覆盖；两档都以本条实测口径为准。
- 队列最多128个未完成job，单owner至多64；满载时不丢旧任务，拒绝新提炼并计数，不影响普通聊天。每条job只处理一个证据，不扫全部history。
- 用索引或有界持久pending ID清单领取任务；不允许每次轮询store.keys全库扫描。实现若使用KV，维护清单与job须同事务。
- 默认持久保存精确来源quote及必要证据关系；全量输入原文仅供在途任务，完成后30天内有界清理或更早删除，只保留支持仍在用事实的最小quote，检查引用可定位。无自动历史回填。敏感内容拒绝后不入提炼证据库。
- queued受理后崩溃必须能恢复。worker启动/关闭归宿主生命周期，一个实例共享主/重模型与眼镜入口，不能每请求生成线程。紧急关闭停止接收新任务；明确区分暂停记录与关闭worker，不谎称清空。

### 5.1 遗忘异步fence（必须实现）

按owner+space维护递增generation。forget事务：递增generation；取消旧generation所有在途job；删除目标事实及其修订链、派生内容/新记忆证据副本；保留无正文source/evidence tombstone及删除ID，完成后缓存失效。只增generation不能阻止旧证据以新job重放，必须再检查evidence tombstone。涉及一证据多事实时，安全优先：撤销该证据的继续提炼资格，已接受的其他非目标事实可保留最小独立quote；删除回应不得承诺不存在的外部记录擦除。

旧generation迟到提交必须拒绝，不自动用新generation重排。后续新用户消息明确重新告知同一事实可以记，是新证据；不是从旧history自动恢复。空间级generation会取消少量无关旧任务，这是首批保守取舍，指标可见，不为提高覆盖率偷偷重排。

只删除结构化事实仍可能被旧chat episode补召，故必须对新事实遗忘涉及的source turn建立召回屏蔽：对应新生成episode/history的记忆注入过滤不得再复述该事实。首批最小方案可在明确遗忘后清空该会话的本地短历史并屏蔽/删除对应源episode，告知它是本地记忆遗忘而非撤回已发消息；具体方式Qwen开工前确认，验收必须覆盖即时下一轮与跨会话。不把“fact表已无记录”当遗忘通过。

## 6. 装配、召回与兼容范围

新事实使用独立tag/namespace（如owner_fact_v1），不要一律挂旧curated以绕过生命周期过滤。新retriever只返回当下被授权、accepted且有效的事实；按相关性挑选后合入companion记忆预算。旧群/curated路径保持行为，但预算对最终memory snippets统一生效。

默认预算：最多8条，单条最多300字符，整个memory snippet payload（含来源标签）最多2400 Unicode字符；有模型tokenizer则同时最多1200 tokens。未接tokenizer明确报告仅字符硬界，不将字符数伪称token数。预算在最终组装点执行，不允许探索/其他append绕过。该预算不声称覆盖全部历史与persona；维持原有历史上限并报告分段统计，不引入常驻块。

默认装配：合格owner入口随宿主启用，不能只给一个默认off的feature模块。提供 `WHALE_OWNER_FACT_MEMORY=0` 紧急开关；授权缺失时fail-closed并有诊断，不把“开关on但所有入口缺auth”当接线完成。没有额外模型配置时复用宿主已配置的文本模型/provider能力；若结构化输出不合格则跳过并可观察，不要求用户另配云记忆服务。

管理兼容采取窄分派：凡能展示/删除新记录的旧入口，对owner_fact_v1必须转新服务，或不展示该类型并提供唯一完整新管理路径；不得旧API直接store.delete后允许迟到job复活。不重构所有旧记忆的治理语义。

## 7. 文件责任和实施顺序

### Kimi：领域实现与worker（先交接口契约小提交）

负责新memory域模块（冻结为 `owner_facts.py` 领域对象/服务/检索，`owner_fact_extraction.py` 模型提炼及校验，`owner_fact_worker.py` 持久任务worker），schema/纯提炼校验、幂等/来源、时间、明确冲突、事务、generation/tombstone、持久jobs、缓存删除契约、离线单测。复用store事务，不新增SQL schema；protocol类型确需补充时由协调者统一修改。不要改companion/渠道API/群extractor。Kimi专属测试为 `tests/test_owner_facts.py`、`tests/test_owner_fact_worker.py`。

先交冻结dataclass/protocol与假实现，列出worker启动/停止、drain测试钩子及授权回调要求；再交完成实现。不要把业务模块拆成互相无法运行的大量脚手架。

### Qwen max：可信入口、装配、检索管理、端到端质量

负责可信owner+glasses适配、server evidence ID复用、宿主单worker装配、companion调用点与最终预算、新事实召回、显式遗忘/更正的窄管理分派及源episode/history抑制、eval/test fixtures、可观测性。主要编辑既有入口文件及新integration/eval，不编辑Kimi拥有的核心事务逻辑；接口不足先反馈，不能另写第二套store直写。

重点检查 [glasses宿主](<../src/whale_core/api/glasses.py>)与[profiles装配](<../src/whale_core/profiles.py>)、[QQ工作流](<../src/whale_core/life/qq_workflow.py>)的owner映射；由实际被部署的composition root决定改动，不能只把测试里构造好的服务塞入companion。

### 集成门

1. 接口+人工合成证据fixture双方通过；协调者确认改动文件责任。
2. 一条真实owner入口普通自述→后台commit→实际companion recall贯通，然后补覆盖；不要先交“仅模块完成”。
3. 更正/遗忘fence+负例+重启与并发测试。
4. 质量/性能基线对照、文档与默认装配审查；协调者决定上线。Astra负责方案审阅，不直接改产品实现。

## 8. 验收矩阵与门槛

测试仅临时SQLite/合成数据，不读用户库。异步测试用明确worker drain/barrier，不sleep赌时序。

必须通过的真实入口场景（至少owner私聊与glasses各一条，不能store.seed替代）：
1. 不带“记住”自述宠物名字、饮食偏好、已完成经历，分别存有来源span与主体；一个消息多个事实全保留。
2. 30/100/300轮干扰后、重启后、同owner另一个授权渠道询问仍命中；未配对设备/其他owner/群不能看到。
3. 助手虚构名字、工具返回“用户喜欢”、引用别人的第一人称、假设/疑问/否定/敏感字段不被升级。
4. 明确自然语言更正只召回新版；新近活动不误伤稳定事实；模糊冲突不擅自替换。
5. 时间锚定、到期状态不再当现状；非法/缺时区日期不捏造。
6. 删除时worker卡在LLM返回前，随后释放：提交拒绝；重试/重启/同旧evidence再次入队/提炼版本变化均不复活；下一轮旧episode/history也不复述遗忘内容。
7. pause/optout/解绑发生在入队后提交前，拒绝写入；无上下文兼容调用不自动写入。
8. 多候选幂等、重复投递、并发更正/delete、崩溃恢复、事务回滚；无半更正、无来源dangling、无任务无限重试。
9. 默认真实composition root装配服务，主回复不等待提炼；满队列/模型超时/异常不使聊天失败；最终预算硬界通过。

质量基准与产品集成测试分开：构建至少60条人工标注合成证据（≥30正例、≥30对抗/不应记），至少30个跨长间隔查询，覆盖事实/偏好/经历/更正/时间/拒答。报告自动accepted precision、写入recall、Recall@5、来源支持率、错误主体率、已删除/过期泄漏率，以及回答引用是否真实支持。

离线确定性门：安全夹具越权、助手晋升、删除复活、过期现状均0；accepted来源完整率100%；预算违规0；正例场景不能全pending冒充安全。录制/固定提炼输出用于可复现系统测试，但另外需要经协调者批准、使用合成数据和真实已配置模型的小样本评估，才能声称语义提炼质量达标。不得用FakeExtractor的分数宣传真实LLM准确率。

上线候选目标：人工标注集accepted precision≥95%，明确自述写入recall≥80%，有效长期查询Recall@5≥90%，所有安全门保持0失败；若模型达不到不调低安全门，收窄自动accepted范围或继续修正。样本数/置信区间和具体失败必须公开，不把小样本目标说成保证。

性能：在同一机器临时SQLite上测无功能基线与启用后，报告主路径observe额外p50/p95、整轮延迟、后台模型调用/耗时/失败、队列深度/恢复。初始工程门为observe p95≤20ms（测试机必须记录），正常负载后台p95≤30s；如未达标说明机器和瓶颈，协调者裁决，不能静默改为同步。现有memory_bench仍用于1k/10k检索开销，不替代质量集。

日志/metrics只含计数、不可反查正文的内部ID、耗时、状态与拒绝原因，不打印用户原文/quote。列表/解释接口向已授权owner展示证据与状态，不向无权限渠道泄漏“有该事实”。

## 9. 尚需Qwen研究/实施前确认的最小事项

- 实际owner私聊和glasses trusted identity→可写space的具体装配点；无法证明同owner时不做自动合并。
- 显式遗忘路径如何最小抑制对应旧episode与短history；已有命令/API哪些能触达新记录。
- 现有model provider最小结构化输出调用接口及真实超时/取消能力，避免worker超时后模型线程永不结束。
- 启动宿主的单例worker归属、退出关闭路径与测试drain接口。

这些影响接口适配，不改变首批范围。任何需迁移生产数据、引入新服务或改群策略的提议均回协调者另批。
