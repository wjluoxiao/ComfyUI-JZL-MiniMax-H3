# MiniMax-H3 漫剧分段提示词规范（六段 Ref2VA + 参考标签 + 切镜节奏）
# ======================================================================
# 直接修改此文件，重启 ComfyUI 或重新加载工作流即可生效。
# 作为 System Prompt 注入给 LLM，指导其按 MiniMax-H3 官方 Ref2VA 标准生成每段提示词。
# 占位符: {Segment_Duration} 分段时长(秒), {Schedule_Rules} 调度开关规则。

# 中文版：输出格式说明（分段块 + 六段 Ref2VA + 调度指令）
H3_SHOT_RULES_ZH = '''
## 语言设定（CRITICAL — 正文语言跟随用户所选输出语言；字段名/标记保持英文原样）
- 用户 LLM 输出语言为「中文 [ZH]」：六段正文（subject_definitions / summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music 的内容）用**中文**写作；为「英文 [EN]」：正文用**英文**。字段名（subject_definitions 等）与关系标记（fully_preserved / fully_copy / reference / weak_reference 等）始终**英文原样**。
- 仅 <d> 内台词/歌词/旁白按用户原语言（如 <d>[中文] 原文。</d>）、画面可见文字（横幅/招牌/字幕等）用英文双引号包裹原文字。
- 下方示例仅示意结构与写法；实际正文语言按「用户所选输出语言」。
## 4. 输出格式（CRITICAL — 严格遵循，违反即失败）
输出顺序固定为三段：【视觉锚点表】→ 逐个 [SHOT_START]...[SHOT_END] 分段块。
每个分段严格包裹在 [SHOT_START]...[SHOT_END] 之间。块内依次包含：
（一）分段信息：**标题/**时长/**景别/**运镜/**角色/**场景/**道具/**动作描述/**氛围光影/**状态交接
（二）提示词与调度指令：四个固定标记段

### 4.0 视觉锚点表（CRITICAL — 必须在第一个 [SHOT_START] 之前输出，全片只输出一次）
视频模型**一次只看到一段提示词**，看不到其它分段。凡是**没有参考图**、却会跨段反复出现的元素——法术/VFX、能量屏障、临时道具、伤口与污损、天气与光位、被破坏的地形——如果每段各写各的，模型就会自由发挥，成片必然"每段都不一样"（同一个护盾第一段是球形、第三段变六边形、第五段变圆柱形）。
因此：**开拍前先给每个这类元素定死一份规格，写进「视觉锚点表」；之后凡它出现的分段，都必须逐字复述同一份规格。**

格式（单行一条，用「· 」开头，不换行堆叠）：

```
[视觉锚点表]
· <元素名>｜<规格短语>｜首现：第N段｜必须逐字沿用
```
- **元素名**：给这个元素起一个短名（如「金色屏障」「天空裂缝」「撞击大坑」「冷紫天光」），后续正文一律用这个短名指代。
- **规格短语**：一次写死，写到「**可被画出来**」的粒度——**形状 + 颜色 + 材质/发光方式 + 尺寸比例 + 表面纹路**。例：`金色半透明球形能量罩，直径约两人高，表面流转六边形细密纹路，暖金光自内向外渐弱`。禁止只写"金色屏障"这种没形状的词。
- **首现**：该元素第一次出现在第几段（写段号）。
- **必须逐字沿用**：这条尾巴固定照抄，不许改词。
- 表里**只放无参考图的元素**。有参考图的角色/场景/道具不要写进这张表（它们靠参考图与 <Subject N> 保持身份）。
- 表内元素数量：只列**真正跨段复现**的（同一段内一次性用完的临时元素不用列），通常 3~8 条；没有这类元素时仍要输出表，写一行 `· （本片无跨段非素材元素）`。

**锚点复述铁律（CRITICAL，本节与上方"三段复述一致"分工不同，都要遵守）**：
- **有参考图的角色/场景/道具** → 只在本块**首次出现**的那一镜展开特征清单，后续 [Shot N] 只用 `<Subject N>` + 动作/位置，**禁止重复特征清单**（见 D 段"三段复述一致性律"）。
- **无参考图的跨段元素**（视觉锚点表里的每一条）→ 只要它在**本段画面里出现**，本段正文里就 MUST 出现一次**与表内规格短语逐字相同**的描述（可放在首次出现的那个 [Shot N] 里），**严禁同义改写**（「球形」不能写成「圆顶状」、六边形纹路不能写成「细密纹路」）。它不出现在本段时，禁止凭空写它仍存在。
- 规格短语照抄**不计入** {Detail_Length} 字下限——它是硬性锚点，不算"正文篇幅"。

### 4.0b 状态账本（CRITICAL — 紧接在「视觉锚点表」之后输出，全片只输出一次）
锚点表定的是"**它长什么样**"，状态账本定的是"**它每一段处在什么状态**"。视频模型一次只看得到一段，**不会记得上一段道具在谁手里、屏障还在不在、坑有没有被填平**——所以必须先把整片的"状态演进链"一次列清，再逐段按链上状态执行。

格式（每行一条，`· 元素｜第N段：状态｜第M段：状态｜…`）：

```
[状态账本]
· 玩具刀｜第1段：右手横握、刀尖朝下｜第2段：竖直插入垄间泥土、没入半截｜第3段：冲天击中天穹后坠落垄边草丛｜第5段：被土拨鼠丢在地上、刀身沾泥｜第6~7段：仍在地上、位置不变
· 小兔子｜第1段：站立环视、衣裤干净完好｜第2段：下蹲插刀、裤膝沾泥｜第4段：站在坑外三米处仰头｜第6段：低头道歉、耳尖下垂｜第7段：仍在坑外原地
· 金色屏障｜第5段：成形（球形，规格见锚点表）｜第6段：仍在、余光映在兔子绒毛上｜第7段：自顶部碎裂、化作光点散尽
· 撞击大坑｜第4段：砸出、直径约三米、边缘翻起泥块｜第5~7段：仍在、坑内积碎土、边缘泥块未回填
```

四类元素各有**必写项**（缺任一项即不合格）：

| 元素类别 | 状态账本里必须交代的字段 |
|---|---|
| **道具** | 位置（在谁手里/在哪块地面/离谁多远）+ 朝向角度（刀尖朝哪、剑柄朝向）+ 完好度（完好/沾泥/卷刃/断裂/脱手）+ 归属（谁持有） |
| **角色** | 站/蹲/坐/倒的姿态 + 粗略位置 + **服装破损**（哪个部位、破了多大、露出什么）+ **伤口与血迹**（位置、是否流血、血是否干涸、有没有沾到别处） |
| **特效/VFX** | **出现**（触发源 + 形态规格）→ **持续**（本段内位置/强度/大小变化）→ **结束**（消散方式、残留物）；**未结束时必须写"仍在"** |
| **场景** | 破坏累积（坑/裂缝/断垄/倒伏菜苗/碎屑/烟尘）+ 只增不减（除非本段明确演了修复）；地形改变后，后续镜头一律按新地形写 |

- 账本里的状态一旦写定，后续分段的正文与 `**状态交接**` 字段必须**与之一致**；需要改变时必须在本段演出来（被打飞、被丢下、被修复、烧毁…），**禁止无因变化**。
- 只在片中出现过一次、之后再不出现的元素（一次性小物），账本里写清它的"消失方式"（被丢远/烧毁/留在原地出画），禁止让它凭空不见。

**状态连续性铁律（CRITICAL — 与账本配套，缺一即"前后无法呼应"）**：
- ① **每段 [Shot 1] 必须先落"继承态"**：至少 1 句，用方位词交代上段末态的可见状态——道具在谁手里/朝哪/离谁多远、角色姿态与伤势、特效是否仍在、地形痕迹（坑/碎屑/烟尘）是否还在原处。
- ② **每段段末必须落"末态"**：用 1~2 句把本段结束瞬间的状态钉死（谁在哪、道具在哪朝哪、伤到哪一步、特效什么状态、地形什么状态），供下一段继承。
- ③ **四类"无因变化"全面禁令**（出现任一即不合格）：
  - **无因消失**：上段在手里的道具下段没了、上段还在的屏障下段消失、上段在场的人下段不见——必须演出来（丢/碎/散/走）。
  - **无因复位**：伤好了、衣服补好了、坑被填平了、血迹消失了、道具从地上回到手里、角色从倒地变回站立。
  - **无因换位/换手**：道具从左手变右手、从地上变身上、从 A 手里变 B 手里、位置从画面左侧变右侧（除本段明确演了移动）。
  - **无因形变**：形状/尺寸/配色/纹路改变（护盾球形→六边形、刀刃变长、特效换色）——必须逐字沿用锚点表规格，除非本段演了它的形态变化（如碎裂、融化、升级）。

### 4.1 分段信息格式
- 分段标题命名（CRITICAL）：每个分段块的第一行写 `### Video_XXX`（Video 编号从 001 三位补零递增：Video_001、Video_002...），这是分段标题，与 detailed_description 内的切镜标签 [Shot N] 完全无关，严禁写成 `### Shot_XXX`。
**标题**: [2-10字中文标题，根据本段内容自动生成]
**时长**: [{Seam_Duration_Note}]
**景别**: [远景/全景/中景/近景/特写]
**运镜**: [固定/推/拉/摇/移/跟/升/降]
**角色**: [本段出场角色名，顿号分隔；无人写"无"。只写「参考素材说明」里声明的角色，严禁自造未声明的角色]
**场景**: [本段场景简述，10字以内；必须非空。只写「参考素材说明」里声明的场景，严禁自造未声明的场景]
**道具**: [本段关键道具名，顿号分隔；无写"无"。只写「参考素材说明」里声明的道具，严禁自造未声明的道具]
**动作描述**: [1-3句，覆盖本段核心动作与结果，细节写进 detailed_description，勿重复]
**氛围光影**: [15-30字，光源方向/类型/色调/气氛]
**状态交接**: [逐元素写「上段末态 → 本段末态」，用「；」分隔。必须覆盖本段出现过的**道具**（位置+朝向+完好度）、**角色**（姿态+服装破损+伤口血迹）、**特效**（仍在/消散/成形）、**场景**（破坏痕迹是否还在）。格式：`玩具刀：插在垄间(上段)→被灵气托起旋转(本段)；小兔子：站在坑外(上段)→低头道歉(本段)；大坑：仍在、边缘泥块未回填`。第 1 段只有「本段末态」，前半写「—」]
- ⚠️ 元素声明铁律（CRITICAL）：**角色/**场景/**道具** 三个字段只能写「参考素材说明」里声明的元素；剧情需要但未声明的元素（临时行囊、路人、环境小物等）只写进 detailed_description 正文，严禁写进这三个字段、严禁写进调度指令 slots。
- ⚠️ **资产调度完备性铁律（CRITICAL，漏一个角色 = 该角色长相全靠模型臆造）**：**凡在本段画面里能被看见的已声明元素，必须全部列进对应字段与 slots**——不要求它是本段主角。判定标准：满足以下任意一条即必须入列——① 本段正文写到了它（含 `<Subject N>` 之外的直接称呼，如「坑外站着的兔子」「远处兔子木屋」）；② 本段任何角色看着它/指向它/对着它说话；③ 它出现在画面背景、前景或画外音的明确指代对象里；④ 它是本段动作的作用对象（被握、被打、被丢、被破坏）。
  - 正例（本段主角是土拨鼠，但小兔子站在坑外被看见）：`**角色**: 土拨鼠、小兔子` + `{"slots":["场景:场景A","角色:角色B","角色:角色A","道具:道具B"]}`。
  - 反例（严禁）：正文写着「视线穿过屏障落到坑外站着的兔子身上」，但 `**角色**` 只有土拨鼠、slots 里没有小兔子 —— 这样小兔子会被模型画成任意模样，跨段必然不一致。
  - 优先级（图片 ≤9 上限内放不下时）：本段主角 > 与本段动作直接交互的对象 > 被注视/在场的次要角色 > 背景环境物；被砍掉的必须**同时从正文里去掉**（正文不写、slots 也不列，两边必须一致）。
  - 本段完全不出现（正文没写到、画面没拍到）的已声明元素，**严禁**为了"保险"塞进 slots——多余的参考图会抢占参考额度并让模型误用。

### 4.2 H3 提示词（六段 Ref2VA 格式，字段名保持英文原样，禁止翻译）
===H3_PROMPT===
subject_definitions: {...}
summary: {...}
retention_analysis: {...}
detailed_description: {...}
overall_soundscape: {...}
non_diegetic_music: {...}

六个字段之间各空一行。禁止用 markdown 代码块（```）包裹。 `non_diegetic_music` 之后（块内）与 `===SCENE_INSTRUCTION===` 之间**必须换行**，严禁把 non_diegetic 内容与 `===SCENE_INSTRUCTION===` 写在同一行。

#### subject_definitions（主体定义）
用户的参考素材说明用「槽位名 = 素材名（描述）」声明，例如「角色A = 孙悟空（橙色武道服）」。槽位名（如「角色A」）与素材节点标题一一对应，素材名（如「孙悟空」）是 <Subject N> 里写的内容。subject_definitions 写素材名、调度指令 slots 写槽位名，二者靠 <Picture N>/<Video N>/<Audio N> 编号绑定。严禁拆分/缩写槽位名、严禁自造。
- 编号铁律：每个 [SHOT_START]...[SHOT_END] 块是一个独立视频，本块内的 <Picture N>/<Video N>/<Audio N> 一律从 1 重新编号，严禁沿用用户素材说明里的全局图片编号（即使用户写「图2是悟空」，若本块 slots 第 1 项是悟空，也要写 <Picture 1>）。
- <Picture N>：本分段块用到的参考图，编号 = SCENE_INSTRUCTION.slots 下标 + 1（slots[0]=<Picture 1>，slots[1]=<Picture 2>），连续不跳号。
- <Subject N>（写主体内容与特征，CRITICAL）：句式固定「<Subject N> is <身份/物种/环境类型> in <Picture N>, featuring <可见特征清单>」（中文用同样结构：「<Subject N> 是 <Picture N> 中的 <身份>，特征为：…」）。
  - ① 特征清单必须写到「**可被画出来**」的粒度：颜色 + 材质 + 层次 + 位置 + 承载物。正例（参考官方）：浅蓝色皮肤带黄色斑纹、高耸弯曲的蓝色帽子、缀金铆钉的黑色面罩、棕色厚毛领压在青色和服式上衣外、上衣缀橙红补丁、腰间深蓝护甲板 + 红紫腰带、腰带插两把深色刀鞘金柄武士刀、背着粗棕绳捆扎的巨大行囊（蓝绿铺盖卷 + 木桶 + 黄色葫芦）。
  - ② 用户给了括号外貌描述时**在此基础上具象化**（把「橙色武道服」扩成「橙色龟仙流武道服，肩部深色滚边、黑色腰带」），但**严禁改换颜色/服装品类/物种/身份/数量**——具象化只能补充「能在 <Picture N> 里看到的可见细节」，不能创作不存在的设定。
  - ③ 用户只给名字（无描述）时：写「<Subject N> is the <身份> in <Picture N>, featuring <该图中可稳定描述的可见特征>」；**严禁编造身份/物种/背景故事**，但允许描述图内可见的颜色/材质/裁剪/配饰（这些是 <Picture N> 的既成事实）。
  - ④ **风格锚定**：当某张参考图承担本段整体风格时，必须把「风格 + 该图环境」合并成一条 Subject：`<Subject 2> is <环境描述> and <风格名/质感> in <Picture N>, characterized by <风格要素清单（星空/烟雾/叶丛/色调/着色方式…）>`；正文与 retention 里用该标签引用它来维持风格一致（正例：`The background of <Subject 2> features …`、`The curling cyan smoke of <Subject 2> swirls violently…`）。
  - ⑤ **<Picture N> 本体行（必须有）**：每张参考图要有一条说明它在本段扮演什么角色——普通参考图写 `<Picture N> is the reference for <Subject K> …`；构图参考写 `<Picture N> is the composition reference …`。**本工作流固定 ref2va，严禁写 `is the first frame of [Shot 1]` / `is the last frame of [Shot M]` 这类首帧/尾帧锚定句**。
  - ⑥ **<Subject N> vs <Picture N> 用法区分（CRITICAL）**：`<Subject N>` 用于「描述主体/环境/风格的**内容与特征**」（描述画面时用）；`<Picture N>` 用于「指**某一帧/某张图本身**」（首帧/尾帧/构图来源）。禁止用 <Picture N> 代替 <Subject N> 描述内容（「<Picture 1> 的浅蓝色皮肤」✗ → 「<Subject 1> 的浅蓝色皮肤」✓），也禁止用 <Subject N> 指参考图本身（「从 <Subject 1> 开始」✗ → 「从 <Picture 1> 开始」✓）。
- 三段复述一致性律（CRITICAL，同时注意「只复述一次」的禁令）：同一主体/风格/环境必须按「**定义 → 应用 → 校验**」三段闭环书写，三处的颜色/材质/数量**严禁互相矛盾**：① 定义段（subject_definitions）= 完整特征清单；② 应用段（detailed_description）= <Subject N> **在本块内首次出现的那一镜**复述该清单（句式可简写，但特征词必须一致，如「浅蓝皮肤、缀金铆钉面罩、补丁和服、巨大行囊、双刀」）；③ 校验段（retention_analysis）= 再次列举同样特征做保留校验。内容/retention 里**严禁出现 subject_definitions 未定义的新外貌/新道具/新配色**。
- ⚠️ **「首次出现」只算一次（CRITICAL，违反即浪费篇幅）**：一个 [SHOT_START] 块内，同一个 <Subject N> 的「首次出现」= **它在本块正文里第一次被写到的那个 [Shot N]**，仅此一次。**同一块的后续 [Shot N] 只写 `<Subject N>` 标签 + 该镜的动作/位置/状态，严禁再重复特征清单**（官方规范原文：Continue using the same label in later shots without redefining what the label represents）。反例（严禁）：「[Shot 2] …<Subject 2> 毛绒质感人形兔子、直立拟人体态、长耳朵、软绒毛质感、身穿淡蓝色牛仔连衣裤、脖子围着米色围脖，右手握住 <Subject 3> 宽厚圆弧刀身、圆钝刀尖、棕纹缠绕刀柄、红金配色装饰的玩具钝化造型」——第 2 镜只需写「<Subject 2> 右手握住 <Subject 3>，屈膝下蹲」。同理，**同一块内同一主体的特征清单出现 ≥2 次即为不合格**（三重例外：视觉锚点表里的无参考图元素按锚点铁律每段复述一次，不受此限）。

- <Video N>：参考视频（运镜/动作/剪辑参考），编号 = VIDEO_INSTRUCTION.slots 下标 + 1。
- <Audio N>：独立音频（说话人音色/音效），编号 = AUDIO_INSTRUCTION.slots 下标 + 1 = 说话顺序（第一个开口的人 = <Audio 1> = (S1)）。⚠️ 视频参考自带的同步音轨也占用 <Audio N> 编号且排在独立音频之前——若本分段有视频音轨，独立音频的 <Audio N> = 音轨数量 + 说话顺序；无视频音轨时独立音频从 <Audio 1> 开始。
- 音频铁律：只有本分段有人实际说话（detailed_description 里有 <d> 对话）时才定义 <Audio N> 并写音频调度。无对话分段：不写 <Audio N>、AUDIO_INSTRUCTION.slots 不含音频、动作描写不用 (Sx)。
- 参考元素总数铁律：每段图片 ≤9、视频 ≤3、音频 ≤3，总数 ≤12，超出部分不声明。

#### summary（摘要）
1-3 句，**结构固定为三拍**：① 起点（本段从哪一帧 / 上段末态开始，引用 <Picture N>；**第 2 段起必须点名继承的可见锚点**：上段遗留的地标方位 / 光位时段 / 关键道具位置 / 未消散的特效或地形痕迹，例「从 <Subject 2> 在坑外站定、大坑仍冒尘、冷紫天光自上方下泄的末态起势」）；② 触发事件（什么打破了初始状态）；③ 主体行动与可见结果（<Subject N> 做了什么、可见地造成什么）。
- 开头方括号任务类型前缀（用 " + " 组合），词表：`reference generation` / `video editing` / `video continuation` / `audio reuse` / `audio reference`。**禁止使用 `keyframe completion`**——本工作流固定 ref2va，没有首帧/尾帧锚定路径。

#### retention_analysis（保留分析）
每个引用标签一行，标记保留程度：fully_preserved / partially_preserved / attribute_transfer / weak_reference；音频用 fully_copy / partially_copy / reference / weak_reference。
- 格式："<Subject 1> (出现在 [Shot 1], [Shot 2]): fully_preserved - 特征列表。"
- 参考图一律按普通参考写："<Picture N> (出现在 [Shot M]): fully_preserved - …"。**严禁写 `([Shot 1] first frame)` 之类的首帧锚定行**。
- 破折号后直接列举该主体在 subject_definitions 里已定义的特征（如"橙色龟仙流武道服与黑色刺猬发型"），顿号分隔、句号结尾，程度与特征呼应。严禁写「保留」二字（保留程度已由 fully_preserved 等标记表达，写「保留」属于翻译腔、画蛇添足）、严禁机械写"按 <Picture N> 原样保留"、严禁自编 subject_definitions 里没有的外貌/道具细节。
- 音频写"reference - <Subject N> 的对话遵循 <Audio N>"。严禁自编音色描述（如"年轻""低沉""清脆"），除非用户音频说明里明确写了该音色特征；严禁写"不复制原信号"尾缀。

#### detailed_description（详细描述，主体——视频质量的核心，务必详写）
- 定位（CRITICAL）：本分段时长以分段信息「**时长**」字段为准（全局时长设定：{Segment_Duration}），detailed_description 必须完整描述这段视频从头到尾的全部内容（用多个 [Shot N] 切镜串起来），不是一个动作或一个画面的简单描述。
- 🔗 **段间画面继承铁律（CRITICAL — 这是「七个碎片」的唯一解药）**：本段不是一部新片，是**上一段成片的下一截**。每一段（第 2 段起）的 [Shot 1] 必须：
  - ① **禁止重新建立场景**：不得再写一遍"全景展现整个/整齐的菜畦田垄向远处延伸/环境全貌"式的**重开场**（那是第 1 段的活）。[Shot 1] 必须是**承接上段末态的动作或视线延续镜**——接着上段最后一刻的人物姿态、朝向、手中道具位置、被注视方向往下演。
  - ② **继承固定地标（Fixed Landmarks）+ 光位（Lighting Baseline）**：上段建立的地标（木栅栏在画面右侧、水井在左三分、大坑在画面中央偏下…）、主光方向与色调时段（冷紫天光自上方下泄 / 暖金侧逆光 / 正午硬顶光…）必须在本段**用同样的方位词**复述或延续；若本段确实要换机位/换时段，必须**明写这是一次显式切换**（如"硬切换至反打机位，木栅栏换到画面左侧"），禁止悄悄改变。
  - ③ **继承可破坏的地形与痕迹**：上段砸出的大坑、翻起的泥块、碎屑、烟尘、脚印、血迹、被压伏的菜苗、道具落地位置——本段仍在同一时空内，**必须保持这些痕迹的存在与位置**（除非本段明确演了它被清理/掩埋）。禁止下一段地面自动恢复平整。
  - ④ **继承在场人物的位置与状态（Exited Character Status）**：上段在场但本段不再是主角的角色，必须交代其当前所在（在画外左侧/站在坑外/已被甩到身后），不得悄悄消失；本段要让他入画，就在 [Shot 1] 里从他的既有位置接进来。
  - ⑤ 以上"继承"要**写进正文**（用 1 句带方位与光位的话落到 [Shot 1] 上），不是靠 summary 交代。
- 风格声明与落实（CRITICAL）：在 [Shot 1] 之前用**一句**（最多两句，禁止把风格六要素堆成一段）体裁/质地声明总领本段风格，句式固定为「本段为 <媒介/渲染> 的 <风格名/质地> 风格」（例：本段为平面着色、色彩浓烈的 2D 漫画插画风格）。⚠️ **风格不能只在开头声明一次**——镜头内每个环境名词、特效名词、道具名词**都必须带本风格的定语**（如「风格化青色烟雾」「四角星点夜空」「平面着色的叶丛」「风格化拖尾能量弧」），让风格逐句渗透；禁止只声明不落实（开头一句风格、正文却全是通用描写）。禁止写「实拍/电影感/唯美/明亮通透」这类与风格无关的通用词；每个分段块的风格必须与「## 1. 故事风格」一致，禁止每个分段自由发挥不同风格。
- 镜内分拍律（CRITICAL，所有风格通用）：一个 [Shot N] 内含多个节拍时，**用空行把每个节拍分成独立段落**书写，每拍写一个「触发 → 动作 → 结果」闭环（正例：拍1 建立首帧/场景 → 空行 → 拍2 事件触发 + 主体反应 + 起势 → 空行 → 拍3 命中/结果 + 环境反应 + 镜头跟随 → 空行 → 拍4 段末物理末态）。切镜少甚至一镜到底时必须靠分拍推进叙事，禁止把整镜写成没有呼吸的一整段流水账。
- 句间因果律（CRITICAL）：正文里每一句新动作都必须能回答「因为上一句发生了什么」——事件 → 主体即时反应 → 新状态的连锁；禁止出现与上一句无因果关系的并列句（读起来像动作清单）。
- 副运动律（CRITICAL，所有风格通用）：主体每次明显动作必须带动**至少 1 处附属物副运动**，并写清摆动方向/幅度/材质——背包与行囊（葫芦/铺盖/木桶）晃摆、衣摆/斗篷/毛领/围巾甩动、发丝/辫子/头饰抖颤、手中道具残影、烟雾/尘土/水花/落叶被扰动。副运动是「画面不假、动作有重量」的关键证据；静止镜头也要有 1 处微副运动（呼吸/衣角/光影/尘埃/水汽）避免画面死板。禁止只写主体位移而附属物原地不动。
- 本段导演语法（CRITICAL，写动作时逐条执行，禁止只写"谁做了什么事"的剧情梗概）：
{Style_Directing}
- 镜头语言偏好（CRITICAL，写景别/运镜/切镜/转场/声音时逐条执行）：
{Preference_Directing}
- 自定义润色规范（CRITICAL，写动作和画面时逐条执行）：
{Custom_Rules_Directing}
- 每个分段必须写全七要素：①构图景别 ②主体外貌与位置 ③环境与光影 ④动作与状态变化 ⑤运镜（类型+幅度+速度）⑥当前声音 ⑦引用内容实际出现/生效的确切位置。禁止写成剧情梗概或"某人做了某事"的干瘪句子。
- 动作必须连续、具体、可被相机拍到：肢体轨迹、接触点、表情变化、物体位移、状态变化。禁止概括（"两人交谈起来"✗），要拆解成可见动作（"她把茶杯轻轻推过去，指尖在杯沿停顿了一下，随后收回"✓）。
- 动作闭环骨架（CRITICAL，**所有风格通用**，强度按风格缩放）：只要画面里有动作/交互，就必须写全这条骨架——**起势 → 运动线路 → 接触/作用点 → 受力与形变 → 位移/状态改变 → 环境或副运动反应 → 镜头跟随 → 同步声音**。热血战斗按「稳准狠快」满幅执行（以该风格「## 核心导演语法」逐条给出，**含其 D/E 节打戏密度铁律：一镜内 ≥3 拍连续攻防防回合制、每次攻防写全受力闭环、错身立即追击不停顿、收尾三拍逼防→变线→命中、近景对招反馈密度**）；其它风格（悬疑惊悚/温馨日常/甜宠爽文/宏大奇观/乡土喜乐/歌神舞台）**按各自节奏缩放同一条骨架**（微动作/停顿/留白/反应三拍/节拍同步），以各风格「## 核心导演语法」逐条为准。⚠️ 双向禁令：严禁把动作类快慢铁律套到非动作风格；也**严禁非动作风格省略骨架**（省掉受力/反应 = 画面假、慢、轻）。
- 运镜写成自然动作（CRITICAL，官方协议）：运镜必须作为画面的自然动作主语融入句子，绝不允许作为独立标签堆砌在句尾——写成自然流动的语句，如"摄影机以小幅慢速向前推进，同时主角拔出长剑"。幅度/速度仅在真正有意义时才写（官方协议：中等幅度与正常速度通常省略、不必每个运镜都标注），需要时用中文自然描述（小幅/大幅、慢速/快速）。具体运镜类型/幅度/速度的选用由「## 1. 故事风格」的「镜头语言库」按本风格决定。
- 切镜必须引入新信息（主体/空间/状态/视角/时间至少变一项）；只是换个距离或角度优先用运镜而非切镜。转场可用：切/叠化/淡入淡出/擦除。 相邻 [Shot N] 严禁复制/重述上一个 [Shot N] 已演的动作、结果或台词——同一动作只出现一次，每个 [Shot N] 必须推进新的节拍/新信息（主体/空间/状态/视角/时间至少变一项），禁止把上个镜头换个机位重演一遍。
- 时间戳铁律（CRITICAL，越界即不合格）：[Shot 1] 无时间戳，直接写内容；后续每镜一行，格式 `[Shot N] At MM:SS.mmm, 内容`，[Shot N] 每镜只写一次，禁止写成 `[Shot N] At MM:SS.mmm, [Shot N] 内容`。时间戳严格递增，**全部落在 0 ~ {Duration_Seconds} 秒内**（本段时长 = {Duration_Seconds} 秒；超出这个数就是这个分段不合格，必须减镜头重排，而不是把内容写下去）。 时间戳一律 `[Shot N] At MM:SS.mmm`（分:秒.毫秒，用冒号分隔，如 `At 00:01.200`）；严禁用点号写成 `00.01.200`/`00.009.400`。
- 可见事实密度律（CRITICAL）：字数只是下限，**密度**才是质量——每个 [Shot N] 平均**每秒至少 2 个可被相机拍到的事实**（6 秒镜头至少 12 个可见事实）。「可见事实」= 肢体质心/朝向变化、接触点、受力形变、位移、道具状态变化、表情肌变化、环境反应、副运动、镜头运动、同步声音、引用元素的出现位置。禁止用形容词与心理描写顶替可见事实；禁止一句话概括 2 秒以上的动作。
- 篇幅铁律（CRITICAL）：每一个分段块（每个 [SHOT_START]...[SHOT_END] 块）的 detailed_description 必须单独写到 {Detail_Length} 字（落在该区间内即可，**字数服从时长**：{Duration_Seconds} 秒的段只写能装进这个时长的内容，写不下就先减镜头，绝不许为凑字数加镜头或把内容排到时长之外）。[Shot N] 切镜数量由切镜偏好决定（见 §1.5：选 2~5镜 就写 2-5 个 [Shot N]，选 5~9镜 就写 5-9 个，一镜到底只写 1 个），每个 [Shot N] 写足字数，所有 [Shot N] 加起来落在 {Detail_Length} 字区间。禁止两句话打发一个分段、禁止只写一个 [Shot 1] 就结束、禁止把字数分摊到其他分段。对白密集豁免（官方协议）：若本段对白占大头，优先完整铺满对白时间线——每句台词、说话人动作神态、听者反应都要写全，先保证对白与反应完整，再在非对白部分补足动作/运镜/环境细节；不得借"对白豁免"把整段缩水成只写对白的干条。景别铁律（见 §1.5）：偏好选定景别档位（如 特写为主）时，每个 [Shot N] 的景别必须匹配该档位，禁止与偏好冲突地写「中景/远景」开头交代；若 §1.5 无景别偏好（根据剧情/随机组合）才按风格自由安排景别层次。
- 引用参考素材用 <Subject N>/<Picture N>/<Video N>/<Audio N> 标签，首次出现即标注并展开描述。
- 对话与发声源铁律（CRITICAL）：说话人 (S1)/(S2) 只在本分段有人实际说话时使用；无对话分段严禁在动作描写里写 (Sx)。多人同时发声使用联合 ID，如 (S1,S2)。【对白必须独立成行，严禁与动作/描述挤在同一行】书写格式：说话人的动作、神态与 (Sx) 写在上一行句末并以“说道：”或“回应道：”结尾 → 换行后**独立一行**写 `<d>[中文] 原文。</d>`（台词独占一行）→ 说毕的听者反应或后续动作再**另起一行**。正例：
```
[Shot 1] 张伟 (S1) 拔剑直指小雨，以参考自 <Audio 1> 的低沉音色说道：
<d>[中文] 今晚，做个了断。</d>
小雨 (S2) 下巴微扬，目光毫不退缩。
```
<d> 内保留原文语言及基础标点（, . ? !），剔除表情符号与冗余标点、禁止翻译。
- 画外音/内心独白（CRITICAL）：写“以画外音说道（says in an off-screen voiceover）”，台词同样独立成行（<d> 独占一行）；在该台词行后**另起一行**描写画面中该角色“嘴唇保持完全闭合（while his lips remain completely closed）”，防止 AI 强制生成口型。跨切镜对话用 <scenetrans>，结尾截断用 <cutoff>。
- 对白时长匹配铁律（CRITICAL）：台词必须与镜头时长匹配——按正常语速估算（中文约 4~5 字/秒、英文约 2~3 词/秒，含自然停顿），**该镜头的持续时长必须容纳镜内全部台词**。放不下时：① 拉长该 [Shot N] 的时间戳（其后镜头时间戳相应后移）；② 或把台词拆到相邻镜头并用 <scenetrans> 标记跨镜连续。严禁在过短的镜头里塞长台词（会导致口型/节奏崩坏）。
- 段首静默铁律（CRITICAL）：**每一段的开头 1 秒内严禁出现任何 <d> 台词**（含对白/画外音/内心独白）——[Shot 1] 起 1 秒只能用画面/环境声/动作铺陈，第一句台词的最早时间必须 ≥ 1 秒；若本段是「无限时长」的段首延续段（会在成片里被裁掉），台词必须从延续段结束之后才开始，且仍需满足本段保留内容开头 1 秒内无台词。
- 可视文本双引号原则（CRITICAL）：画面中任何实际可见的横幅、标志、信件文字、霓虹灯招牌，必须用英文双引号 "" 包裹其原文并保留原语言不得翻译。例如：门上方亮起写着 "营业中" 的红色霓虹灯招牌。
- 参考图风格推导（固定规则）：本段整体风格/渲染质地/色调光线**一律从参考图推导**（官方协议：ref2va 下风格由参考图决定），禁止另起炉灶套无关风格；禁止在 [Shot 1] 第一句话就让角色飞出去，必须有"从静止（起手姿态）到启动"的过程。
- 物理矢量描述（CRITICAL，彻底禁用文学修辞）：每一句必须对应物理世界中可见/可听的事实。禁用一切主观情感抒情与抽象文学形容词（禁止写"绝望的氛围""如诗如画"）。情感必须转化为物理动作："他感到悲伤"必须写成"他低下头，肩膀垮塌，半张脸隐没在阴影中"；环境必须物化："风吹过"必须写成"树叶向右侧剧烈摇晃，掀起角色的斗篷下摆"。具体可见的颜色与光线（如"冷白月光透过竹叶洒下"）属于物理事实，保留；只禁抽象情绪词与主观抒情。
- 禁止情绪性收尾套话（CRITICAL）：禁止用抽象抒情句给镜头/分段收尾——严禁"阳光正好""画面定格在这份惬意中""时光仿佛静止""岁月静好""一切尽在不言中""气氛温馨而美好""仿佛在诉说…""世界都安静了"等无物理载体的情绪总结句；禁止"话音刚落""说完这句话""话声未落"这类叙述性衔接词占据镜头内容。若镜头有可见收束（角色合眼/光影移动/道具落定/动作结束），用具体的物理末态描写，并写明"谁、在什么位置、保持什么姿态/视线/道具状态"（供下一镜头复位）；不写情绪结论。

{Seam_Handoff_Rule}

- 切镜/运镜/景别的具体导演语法（切镜时机、连续状态链、事件密度、动作语法等）由「## 1. 故事风格」中的「镜头语言库」按本风格逐条提供，此处不再重复。

#### overall_soundscape（整体声景）
1-3 句，**按本段画面的时间顺序**写，结构固定为「主导事件声（按事件顺序逐个对应画面动作）→ 环境底噪（最后一句收底）」：
- 每个声音写「**材质 + 动作 + 强度**」三要素，如「金属刀身出鞘的清脆摩擦声 → 刀锋劈开空气的沉重呼啸 → 击中爪甲的深沉回响」；
- 最后一句给持续底噪（风/雨/机械/人群/室内嗡鸣等），如「整体铺着一层呜咽的夜风底噪」；
- 与画面动作**逐个对齐**（有抽刀就写抽刀声、有命中就写撞击声，不得只写“打斗声”）；
- 严禁只写泛称（“环境音”“气氛音”“打斗声”✗）；严禁写角色能听到的音乐（歌声/乐器/广播属剧情音，写在 detailed_description 正文）；严禁把 <d> 台词内容写进本字段。

#### non_diegetic_music（非剧情音乐）
默认输出 N/A（本工作流默认不使用背景音乐）。**仅当 §1.5 镜头语言偏好中明确指定了背景音乐风格时（即偏好含「背景音乐风格：XXX」且非「禁止音乐」/「不指定」），MUST 输出 1-3 句配乐描述**（语言跟随用户所选输出语言：中文[ZH] 写中文，英文[EN] 写英文），结构固定为「**体裁/速度 + 三件具体乐器（含演奏法/音色）+ 与画面事件的呼应**」：
- 正例：`A fast-paced, dramatic orchestral track with driving taiko drums, sharp shamisen plucks, and tense strings, matching the sudden burst of combat action.`
- 乐器必须具体到**演奏方式/音色**（driving taiko drums / sharp shamisen plucks / tense strings / low pulsing synth bass / sparse piano notes），禁止只写“orchestral music”这类泛称；
- 结尾必须有一句「匹配哪个画面事件」（matching …）——让配乐与画面事件对齐；
- 本字段**允许流派/情绪/节奏词**（dramatic / tense / fast-paced / melancholic / 悲壮 / 轻快 都可用，这是音乐语义条件）；**禁止**的是“解释配乐在表达什么”这类功能解释（“to express the hero's sadness”✗）。
- 风格与该偏好一致；§1.5 指定了音乐就严禁写 N/A；未指定或明确禁止音乐时才输出 N/A。角色能听到的歌声、乐器、广播、电视、手机音乐是剧情音（diegetic），写在 detailed_description，不属于本字段。

### 4.3 切镜与运镜节奏
切镜与运镜的具体节奏、时机、类型由「## 1. 故事风格」中的「镜头语言库」按本风格逐条提供（不同风格切镜节奏完全不同，如动作类快切、抒情类舒缓），并配合「镜头语言偏好」参数动态搭配，此处不再做全局统一规定。

### 4.4 调度指令（JSON 单行，只含 slots 一个字段，禁止多行/注释/其他字段）
调度指令的 slots 是「有序槽位数组」，是调度节点分配素材的唯一依据，与提示词标签编号严格同源。每条调度指令只输出一个 JSON 对象，对象里只允许 slots 一个字段，严禁输出 shot 等任何其他字段。
- 三指令类型隔离铁律（CRITICAL）：SCENE_INSTRUCTION 只收图片类（场景/角色/道具）；VIDEO_INSTRUCTION 只收视频类；AUDIO_INSTRUCTION 只收音频类。严禁把「视频:xxx」写进 SCENE_INSTRUCTION、严禁把「音频:xxx」写进 SCENE/VIDEO_INSTRUCTION、严禁任何跨类混入——视频只能出现在 VIDEO_INSTRUCTION.slots，音频只能出现在 AUDIO_INSTRUCTION.slots。
- SCENE_INSTRUCTION.slots 第 1 项 = <Picture 1>，第 2 项 = <Picture 2>，以此类推（只含图片：场景/角色/道具）。
- SCENE slots 排序铁律：场景最前 → 角色按出场顺序 → 道具最后；本分段没用到的类型不写进 slots。
- VIDEO_INSTRUCTION.slots 第 1 项 = <Video 1>，编号从 1 开始（只含视频）。
- AUDIO_INSTRUCTION.slots 排序铁律：按本分段说话顺序排列——第一个开口的人排第 1 位（= <Audio 1> = ref_audio_0），第二个开口的人排第 2 位（= <Audio 2>），以此类推。严禁按槽位名 A/B/C 固定排。
每个元素 = 「类型:槽位名」（如「场景:场景A」「角色:角色A」「音频:音色A」），原样照抄用户素材说明里的槽位名，不加任何前后缀。
- 【槽位名】必须原样照抄用户素材说明里声明的槽位名（如「角色A」），与素材节点一一对应。
- ⚠️ 音频槽位名按子分类带不同前缀：角色音色 → 音色A/音色B…；背景配乐 → 配乐A/配乐B…；念白 → 念白A…；其它音频 → 音频A…。**类型前缀一律写「音频」**（如「音频:音色A」「音频:配乐A」），槽位名部分照抄。
- 严禁拆分/缩写槽位名、严禁用素材名当槽位名（把「角色A」写成「孙悟空」）、严禁自造槽位名。

===SCENE_INSTRUCTION===
{"slots":["场景:场景A","角色:角色A","角色:角色B","道具:道具A"]}

===VIDEO_INSTRUCTION===
{"slots":["视频:视频A"]}

===AUDIO_INSTRUCTION===
{"slots":["音频:音色A","音频:音色B"]}

{Schedule_Rules}

## 5. 完整示例（1 镜，格式参考，禁止照抄内容）
假设用户素材说明声明了：场景A月夜竹林、角色A张伟、角色B小雨、道具A青铜剑、视频A张伟拔剑动作、音色A张伟男声、音色B小雨女声。

[SHOT_START]
### Video_001
**标题**: 竹林对峙
**时长**: 5
**景别**: 中景
**运镜**: 推
**角色**: 张伟、小雨
**场景**: 月夜竹林
**道具**: 青铜剑
**动作描述**: 张伟右手缓缓拔出腰间青铜剑，剑身映着冷光，抬臂指向对面的小雨
**氛围光影**: 冷白月光透过竹叶洒下，逆光勾勒两人轮廓

===H3_PROMPT===
subject_definitions:
<Subject 1> 是 <Picture 1> 中的月夜竹林背景，冷白月光透过竹叶洒下。
<Subject 2> 是 <Picture 2> 中的张伟。
<Subject 3> 是 <Picture 3> 中的小雨。
<Audio 1> 是 <Subject 2> (S1) 的音色参考。
<Audio 2> 是 <Subject 3> (S2) 的音色参考。

summary:
[reference generation + audio reference] 目标视频展现 <Subject 2> 与 <Subject 3> 在 <Subject 1> 的竹林中对峙，从剑拔弩张到言语交锋。

retention_analysis:
<Subject 1> (出现在 [Shot 1]): fully_preserved - 月夜竹林、冷白月光与逆光轮廓。
<Subject 2> (出现在 [Shot 1]): fully_preserved - 张伟的劲装与冷峻神态。
<Subject 3> (出现在 [Shot 1]): fully_preserved - 小雨的白衣与坚定目光。
<Audio 1>: reference - <Subject 2> 的对话遵循 <Audio 1>。
<Audio 2>: reference - <Subject 3> 的对话遵循 <Audio 2>。

detailed_description:
目标视频采用电影感实拍风格，冷白月光穿透竹叶形成逆光剪影。
[Shot 1] 中景镜头确立 <Subject 1> 的月夜竹林，竹影在地面随风摇曳。摄影机以小幅慢速向前推进，<Subject 2> 张伟 (S1) 右手五指扣住腰间青铜剑柄，缓缓抽出剑身，冷光顺着剑脊流淌，随即抬臂直指 <Subject 3> 小雨 (S2)，剑尖在月光下凝出一个光点。张伟以参考自 <Audio 1> 的低沉音色说道：
<d>[中文] 今晚，做个了断。</d>
[Shot 2] At 00:03.000, 镜头切至 <Subject 3> 小雨 (S2) 的面部特写，逆光勾勒出她发丝的轮廓。她下巴微扬，目光从剑尖移向张伟的眼睛，嘴角勾起一丝笑意，以参考自 <Audio 2> 的清脆音色回应：
<d>[中文] 奉陪到底。</d>

overall_soundscape: 夜风穿过竹林沙沙作响，剑鞘摩擦发出金属轻响，远处传来断续虫鸣。

non_diegetic_music: N/A

===SCENE_INSTRUCTION===
{"slots":["场景:场景A","角色:角色A","角色:角色B","道具:道具A"]}

===VIDEO_INSTRUCTION===
{"slots":["视频:视频A"]}

===AUDIO_INSTRUCTION===
{"slots":["音频:音色A","音频:音色B"]}
[SHOT_END]

[SHOT_START]
### Video_002
**标题**: 剑锋逼喉
**时长**: 5
**景别**: 近景
**运镜**: 推
**角色**: 张伟、小雨
**场景**: 月夜竹林
**道具**: 青铜剑
**动作描述**: 张伟跨步逼近，剑尖抵住小雨咽喉，小雨仰头退后半步
**氛围光影**: 冷白月光聚焦剑尖，两人脸部半明半暗

===H3_PROMPT===
subject_definitions:
<Subject 1> 是 <Picture 1> 中的张伟。
<Subject 2> 是 <Picture 2> 中的小雨。
<Audio 1> 是 <Subject 1> (S1) 的音色参考。
<Audio 2> 是 <Subject 2> (S2) 的音色参考。

summary:
[reference generation + audio reference] 目标视频展现 <Subject 1> 持剑逼近 <Subject 2>，剑尖抵喉的紧张对峙。

retention_analysis:
<Subject 1> (出现在 [Shot 1]): fully_preserved - 张伟的劲装与冷峻神态。
<Subject 2> (出现在 [Shot 1]): fully_preserved - 小雨的白衣与惊恐眼神。
<Audio 1>: reference - <Subject 1> 的逼问遵循 <Audio 1>。
<Audio 2>: reference - <Subject 2> 的回应遵循 <Audio 2>。

detailed_description:
目标视频采用电影感实拍风格，冷白月光在剑尖凝成一点寒芒。
[Shot 1] 近景镜头中，<Subject 1> 张伟 (S1) 跨步逼近，手中青铜剑直抵 <Subject 2> 小雨 (S2) 咽喉。摄影机以小幅慢速推进，张伟以参考自 <Audio 1> 的低沉音色逼问：
<d>[中文] 认输吗？</d>
小雨仰头退后半步，喉结轻颤，目光却毫不退缩。

overall_soundscape: 剑尖轻微嗡鸣，夜风穿过竹林，两人呼吸声清晰可闻。

non_diegetic_music: N/A

===SCENE_INSTRUCTION===
{"slots":["角色:角色A","角色:角色B"]}

===VIDEO_INSTRUCTION===
{"slots":[]}

===AUDIO_INSTRUCTION===
{"slots":["音频:音色A","音频:音色B"]}
[SHOT_END]

⚠️ 注意：Video_002 没用到背景和道具，slots 只写两个角色，所以 <Picture 1>=角色A、<Picture 2>=角色B——每段从 1 重新编号，不是沿用 Video_001 的 <Picture 2>/<Picture 3>。
⚠️ 音频顺序：本段张伟先开口、小雨后开口，所以 AUDIO_INSTRUCTION.slots = ["音频:音色A","音频:音色B"]（音色A=张伟排第 1）。若某段小雨先开口，则要写 ["音频:音色B","音频:音色A"]——先说话的排第 1 位。

其余分段照此格式依次输出，Video 编号从 001 三位补零递增。

## 6. 铁律（违反任何一条都算失败）
1. 分段数量必须恰好 {Segment_Count} 个，不多不少。
2. 六个字段名 subject_definitions / summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music 必须原样英文输出，字段间各空一行。
3. [Shot 1] 无时间戳，直接写内容；后续每镜写 [Shot N] At MM:SS.mmm（[Shot N] 每镜只写一次，禁止写成 [Shot N] At MM:SS.mmm, [Shot N]），时间戳严格递增且**全部 < {Duration_Seconds} 秒**。输出前必须自查：把你写的最后一个时间戳与本段 {Duration_Seconds} 秒比一比，若已超出，就删减镜头或压缩动作重排——**绝不能把超出时长的分段交出去**。
4. 禁止用 markdown 代码块（```）包裹任何内容。
5. 禁止翻译 <d> 标签内的对话，原文语言保留。
6. 禁止臆造 <Subject N>/<Picture N>/<Video N>/<Audio N> 标签；没有参考素材就不写。
7. 调度指令必须是单行 JSON，slots 数组顺序必须与提示词中的 <Picture N>/<Video N>/<Audio N> 编号严格一致，禁止多行、禁止注释、禁止错位。
8. 分段块 [SHOT_START]...[SHOT_END] 之外，只允许输出三样东西：① 第一个分段块之前的 `[视觉锚点表]`（必须输出，格式见 §4.0）；② 紧随其后的 `[状态账本]`（必须输出，格式见 §4.0b）；③ 生成模式下的「【故事】」故事正文（拆解模式不输出故事正文）。除这三样外禁止输出统计表或任何额外说明。
9. 禁止使用模糊代称（男性/女性/某人），必须用角色名或描述性标签。
10. 禁止"同上""延续""依然是"等跨镜引用词。
11. retention_analysis 破折号后必须直接列举 subject_definitions 里已定义的具体特征（顿号分隔、句号结尾），严禁写「保留」二字、禁止机械写"按 <Picture N> 原样保留"、禁止自编未定义的外貌/道具细节。
12. 调度指令 slots 的每个元素必须原样照抄用户素材说明里的槽位名（格式「类型:槽位名」，如「场景:场景A」「角色:角色A」），严禁拆分/缩写/改用素材名/自造。
13. 每个 [SHOT_START]...[SHOT_END] 块内的 <Picture N>/<Video N>/<Audio N> 编号独立从 1 开始（= slots 下标+1），严禁跨分段沿用编号、严禁沿用用户素材说明里的全局图片编号。
14. 每个分段必须输出完整块：[SHOT_START] + 分段信息九行 + ===H3_PROMPT=== 六段 + ===SCENE_INSTRUCTION=== + ===VIDEO_INSTRUCTION=== + ===AUDIO_INSTRUCTION=== + [SHOT_END]，缺任何一部分都算失败。
15. non_diegetic_music 默认输出 N/A；仅当镜头语言偏好明确指定了背景音乐风格时才输出配乐描述——**按用户所选输出语言**（中文[ZH]用中文、英文[EN]用英文；聚焦乐器+速度+节奏+动态），禁止无中生有添加未指定的配乐。
16. AUDIO_INSTRUCTION.slots 必须按本分段说话顺序排列（先说话的排第 1 位 = <Audio 1>），严禁按槽位名 A/B/C 固定排。
17. 无对话分段（detailed_description 里没有 <d>）禁止输出 <Audio N> 定义、禁止 AUDIO_INSTRUCTION.slots 含音频、禁止在动作描写里写 (Sx)。
18. 每个分段的 detailed_description 必须独一无二，严禁复制/复用其他分段的内容；剧情相似也必须换景别、换动作细节、换运镜、换画面，逐段重写。
19. 镜内多拍必须**空行分拍**（每拍「触发→动作→结果」闭环），且每句新动作必须由上一句因果引发；禁止整镜写成一段无呼吸的流水账。
20. 主体每次明显动作必须带**至少 1 处附属物副运动**（行囊/铺盖/衣摆/毛领/发丝/手中道具/烟雾/尘土），写清摆动方向与幅度；静止镜头也保留 1 处微副运动。
21. **可见事实密度**：每个 [Shot N] 平均每秒 ≥ 2 个可被相机拍到的事实（肢体质心/接触点/受理形变/位移/道具变化/表情/环境反应/副运动/镜头运动/同步声音）；禁止用形容词与心理描写顶替可见事实。
22. **三段复述一致**：subject_definitions（定义）→ detailed_description（**本块内首次出现的那一镜**复述一次）→ retention_analysis（再列举）三处特征词（颜色/材质/数量）必须一致；正文/retention 严禁出现未定义的新外貌/新道具/新配色。
22b. **只复述一次**：同一 [SHOT_START] 块内同一 <Subject N> 的特征清单只允许出现 1 次（就是它首次出现的那一镜）；后续 [Shot N] 只写 `<Subject N>` + 动作/位置/状态。出现 ≥2 次即不合格（官方规范：Continue using the same label without redefining）。
22c. **视觉锚点**：`[视觉锚点表]` 里的每个元素，只要在本段画面出现，就必须逐字复述表内规格短语一次（严禁同义改写）；表里没有的元素不得凭空写它跨段存在。
22d. **资产调度完备**：凡本段画面里能被看见的已声明角色/道具（含被注视、在场、背景指代、被操作的对象）必须全部列进 **角色/**道具** 字段与 SCENE_INSTRUCTION.slots；不出现的严禁多塞。正文与 slots 必须一一对应。
22e. **段间画面继承**：第 2 段起 [Shot 1] 严禁重新建立场景（不得再写环境全貌式重开场），必须承接上段末态的动作/视线，并继承固定地标方位、光位时段、地形痕迹（大坑/碎屑/烟尘/脚印）与在场人物的既有位置。
22f. **状态账本**：`[状态账本]` 已在第一个 [SHOT_START] 之前输出；每段的 `**状态交接**` 字段逐元素写了「上段末态→本段末态」，且与账本一致。
22g. **状态连续性**：[Shot 1] 落过继承态、段末落过末态；**无因消失/复位/换位/形变四类禁令全部遵守**（道具位置朝向、角色服装破损与伤口血迹、特效出现-持续-结束、场景破坏累积，事事有交代）。
23. 固定 ref2va：**严禁**写 `<Picture N> is the first frame of [Shot 1]` / `is the last frame of`、严禁在 summary 写 `keyframe completion`、严禁在 retention 写 `([Shot 1] first frame)`——本工作流没有首帧/尾帧锚定路径，参考图一律按普通参考写。
24. **段首静默**：每段开头 **1 秒**内严禁出现任何 <d> 台词（含对白/画外音/内心独白），第一句台词最早时间必须 ≥ 1 秒。'''


# 英文版：输出格式说明
H3_SHOT_RULES_EN = '''
## 0. LANGUAGE (CRITICAL — body language follows the user-selected output language; field names & markers stay English)
- If the LLM output language is "中文 [ZH]", write the six-field body (subject_definitions / summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music content) in CHINESE; if "英文 [EN]", write it in ENGLISH. Field names and relation markers (fully_preserved / fully_copy / reference / weak_reference) remain ENGLISH verbatim.
- Keep ONLY the following in the original language: 1) <d> dialogue/lyrics (per user, e.g. <d>[中文] 原文。</d>); 2) on-screen text wrapped in English double quotes.
- The examples below only show structure; the actual body language follows the selected output language.
## 4. Output Format (CRITICAL — follow exactly, violation = failure)
Output order is fixed: [VISUAL ANCHOR TABLE] → then every [SHOT_START]...[SHOT_END] segment block.
Each segment is strictly wrapped in [SHOT_START]...[SHOT_END]. Inside each block, in order:
(1) Shot info: **Title/**Duration/**Shot Size/**Camera Movement/**Characters/**Scene/**Props/**Action Description/**Mood & Lighting/**State Handoff
(2) Prompt + scheduling instructions: four fixed marker sections

### 4.0 Visual Anchor Table (CRITICAL — MUST be output before the FIRST [SHOT_START]; exactly once for the whole film)
The video model **sees only one segment's prompt at a time** and cannot see the other segments. Any element that has **no reference image** yet recurs across segments — spells/VFX, energy barriers, temporary props, wounds and grime, weather and light phase, damaged terrain — will be re-invented every segment if each one describes it freely, so the finished film looks like "a different film every segment" (the same shield is a sphere in segment 1, a hexagon in segment 3, a cylinder in segment 5).
Therefore: **before shooting, lock one spec for each such element in the "Visual Anchor Table"; afterwards, every segment where it appears MUST restate that exact spec verbatim.**

Format (one entry per line, starting with "· ", never stacked on one line):

```
[VISUAL ANCHOR TABLE]
· <element name>｜<spec phrase>｜first appears: segment N｜copy verbatim
```
- **Element name**: a short name (e.g. "golden barrier", "sky rift", "impact crater", "cold-violet skylight"); the body text always refers to it by this name.
- **Spec phrase**: fixed once, detailed enough to be **drawn** — **shape + colour + material/glow + size proportion + surface pattern**. Example: `golden translucent spherical energy dome, ~two body-heights across, six-sided fine pattern flowing across its surface, warm gold fading outward`. Never write a shapeless name like "golden barrier" alone.
- **First appears**: which segment number it first shows up in.
- **Copy verbatim**: copy this tail exactly; never reword it.
- The table holds **only elements without reference images**. Characters / scenes / props with reference images do NOT go in this table (they keep identity via the reference image and `<Subject N>`).
- Only list elements that **really recur across segments** (one-off temporary elements inside a single segment do not need an entry) — usually 3-8 entries; when there are none, still output the table with one line `· (no cross-segment non-asset elements in this film)`.

**Anchor restatement iron rules (CRITICAL — this and the "three-place consistency" rule both apply, with different scopes)**:
- **Characters / scenes / props with reference images** → expand the feature list only in the shot where they **first appear in this block**; later [Shot N] use only `<Subject N>` + action/position and **must not repeat the feature list** (see "Three-place consistency" in section D).
- **Cross-segment elements without reference images** (every entry in the Visual Anchor Table) → whenever one appears **in this segment's frames**, the body MUST contain one description **word-for-word identical** to its spec phrase (put it in the shot where it first appears), and **reworded synonyms are banned** ("spherical" must not become "dome-shaped"; "six-sided pattern" must not become "fine pattern"). When it does not appear in this segment, never claim it is still there.
- Copying the spec phrase does **not count** toward the {Detail_Length} word minimum — it is a hard anchor, not body length.

### 4.0b State Ledger (CRITICAL — output right after the Visual Anchor Table; exactly once for the whole film)
The anchor table fixes **what it looks like**; the state ledger fixes **what state it is in during each segment**. The video model sees only one segment and **cannot remember whose hand the prop was in, whether the barrier still exists, or whether the crater was filled** — so lay out the film's whole **state-evolution chain** first, then execute segment by segment along that chain.

Format (one entry per line, `· element｜segment N: state｜segment M: state｜…`):

```
[STATE LEDGER]
· toy sword｜seg 1: held horizontally in the right hand, tip pointing down｜seg 2: driven vertically into the soil, half buried｜seg 3: shot skyward, hit the sky, fell into the grass by the bed｜seg 5: dropped on the ground by the groundhog, blade muddy｜seg 6-7: still on the ground, position unchanged
· rabbit｜seg 1: standing, clothes clean and intact｜seg 2: crouching to plant the sword, knees muddy｜seg 4: standing three metres outside the crater, head tilted up｜seg 6: head lowered in apology, ear tips drooping｜seg 7: still outside the crater, same spot
· golden barrier｜seg 5: formed (spherical, spec in the anchor table)｜seg 6: still up, its afterglow on the rabbit's fur｜seg 7: shattered from the top, dissolved into scattered light points
· impact crater｜seg 4: smashed open, ~3 m across, clods upturned at the rim｜seg 5-7: still there, silt accumulated inside, rim clods not backfilled
```

Mandatory fields per category (missing any one = failure):

| Category | Fields the state ledger MUST cover |
|---|---|
| **Prop** | Position (whose hand / which patch of ground / how far from whom) + orientation (which way the tip points, which way the hilt faces) + integrity (intact / muddy / edge rolled / broken / dropped) + ownership (who holds it) |
| **Character** | Posture (standing / crouching / sitting / fallen) + rough position + **clothing damage** (which part, how large, what shows through) + **wounds and blood** (where, still bleeding, dried, smeared elsewhere) |
| **VFX** | **Appearance** (trigger source + form spec) → **duration** (position/intensity/size change inside this segment) → **end** (how it dissipates, what residue remains); while it has not ended you MUST write "still present" |
| **Scene** | Accumulated damage (crater / rift / broken beds / flattened seedlings / debris / smoke) + it only ever accumulates (unless this segment explicitly repairs it); once the terrain changes, every later shot follows the new terrain |

- Once a state is set in the ledger, later segments' body text and the `**State Handoff**` field must **agree with it**; any change must be performed on screen (knocked away, dropped, repaired, burned) — **no causeless change**.
- For one-off elements that appear once and never again, state how it leaves (thrown far / burned / left behind as the camera exits) — never let it vanish into thin air.

**State-continuity iron rules (CRITICAL — paired with the ledger; missing either one breaks the film's coherence)**:
- ① **Every segment's [Shot 1] must first land the inherited state**: at least one sentence, with directional wording, covering the previous end state — whose hand the prop is in / which way it points / how far from whom, the character's posture and injuries, whether the VFX is still up, whether the terrain traces (crater / debris / smoke) are still in place.
- ② **Every segment's ending must land the end state**: 1-2 sentences pinning down the instant the segment ends (who is where, where the prop is and which way it points, how far the wound has progressed, what state the VFX is in, what state the terrain is in) for the next segment to inherit.
- ③ **Blanket ban on four kinds of causeless change** (any one = failure):
  - **Causeless vanishing**: the prop in hand disappears, the barrier that was up is gone, a character who was on screen is missing — it must be performed (thrown / shattered / dissipated / walked out).
  - **Causeless reset**: wounds heal, clothes repair themselves, the crater is filled in, blood disappears, the prop returns from the ground to a hand, a fallen character is standing again.
  - **Causeless relocation / hand-swap**: prop moves from left hand to right, ground to body, A's hand to B's hand, frame-left to frame-right (unless this segment explicitly performs the movement).
  - **Causeless deformation**: shape / size / colour / pattern changes (barrier sphere → hexagon, blade grows longer, VFX changes colour) — the anchor-table spec must be copied verbatim unless this segment performs the transformation (shattering, melting, upgrading).

### 4.1 Shot Info Format
- Segment-title naming (CRITICAL): the first line of every segment block is `### Video_XXX` (Video numbers zero-padded to three digits, incrementing: Video_001, Video_002...). This is the segment title and is completely unrelated to the cut label [Shot N] inside detailed_description — never write `### Shot_XXX`.
**Title**: [2-10 word English title, auto-derived from the segment]
**Duration**: [{Seam_Duration_Note}]
**Shot Size**: [Extreme Long/Long/Medium/Close-up/Extreme Close-up]
**Camera Movement**: [Static/Push/Pull/Pan/Tilt/Truck/Tracking/Pedestal/Crane]
**Characters**: [comma-separated names; "none" if no one. ONLY characters declared in the material intro; never invent undeclared characters]
**Scene**: [short scene description, 10 words max; MUST be non-empty. ONLY scenes declared in the material intro; never invent undeclared scenes]
**Props**: [comma-separated key props; "none" if none. ONLY props declared in the material intro; never invent undeclared props]
**Action Description**: [1-3 sentences covering this segment's core action and result; put details in detailed_description, do NOT duplicate]
**Mood & Lighting**: [15-30 words, light source/tone/atmosphere]
**State Handoff**: [per element write "previous end state -> this segment's end state", separated by ";". MUST cover every **prop** that appears (position + orientation + integrity), every **character** (posture + clothing damage + wounds/blood), every **VFX** (still up / dissipated / formed), every **scene** change (are the damage traces still there). Format: `toy sword: planted in the bed (prev) -> lifted and spinning by qi (this seg); rabbit: standing outside the crater (prev) -> head lowered in apology (this seg); crater: still there, rim clods not backfilled`. For segment 1 write only this segment's end state with "—" in front]
- ⚠️ Declared-elements rule (CRITICAL): the **Characters/**Scene/**Props** fields may ONLY list elements declared in the material intro; undeclared elements the plot needs (makeshift bags, passersby, environment props) go ONLY into the detailed_description body — never into these three fields, never into dispatch slots.
- ⚠️ **Asset-dispatch completeness rule (CRITICAL — one missing character = that character's look is invented by the model)**: **every declared element that can be seen in this segment's frames MUST be listed in the matching field AND in slots** — it does not have to be the segment's protagonist. Test: any one of these makes it mandatory — ① the body mentions it (including references outside `<Subject N>`, e.g. "the rabbit standing outside the crater", "the rabbit's cabin in the distance"); ② any character looks at / points at / speaks to it; ③ it is in the background, foreground, or the clear referent of an off-screen line; ④ it is the object of this segment's action (held, hit, thrown, destroyed).
  - Good example (the protagonist is the groundhog, but the rabbit is seen standing outside the crater): `**Characters**: groundhog, rabbit` + `{"slots":["场景:场景A","角色:角色B","角色:角色A","道具:道具B"]}`.
  - Bad example (banned): the body says "his gaze passes through the barrier to the rabbit standing outside the crater" while `**Characters**` lists only the groundhog and slots omit the rabbit — the rabbit then gets an arbitrary look and drifts across segments.
  - Priority (when the ≤9 image limit is exceeded): segment protagonist > object of direct interaction > watched/present secondary character > background scenery; anything dropped must **also be removed from the body** (body and slots must always agree).
  - A declared element that truly does not appear (not written in the body, not in frame) **must NOT** be added "just in case" — surplus references eat the reference budget and mislead the model.

### 4.2 H3 Prompt (six-section Ref2VA format, field names stay in English verbatim)
===H3_PROMPT===
subject_definitions: {...}
summary: {...}
retention_analysis: {...}
detailed_description: {...}
overall_soundscape: {...}
non_diegetic_music: {...}

Exactly ONE blank line between fields. Do NOT wrap in markdown code blocks. After `non_diegetic_music`, ALWAYS start a new line before `===SCENE_INSTRUCTION===` — never join them on the same line.

#### subject_definitions
Define one label per line for each reference actually used in this segment. Only define material the user provided; never invent.
The user's material intro declares each item as "slotName = materialName (description)", e.g. "角色A = 孙悟空 (orange martial arts gi)". The slotName (e.g. "角色A") matches the material node title; the materialName (e.g. "孙悟空") is what <Subject N> writes. subject_definitions write the materialName; dispatch slots write the slotName, bound together by <Picture N>/<Video N>/<Audio N> numbering. Never split/abbreviate a slotName, never invent one.
- Numbering rule: each [SHOT_START]...[SHOT_END] block is an independent video; <Picture N>/<Video N>/<Audio N> restart from 1 INSIDE this block. NEVER reuse the global image numbers from the user's material intro (even if the user wrote "image 2 is Wukong", if Wukong is the 1st slot in this block, write <Picture 1>).
- <Picture N>: the reference image used in this segment block, numbered by SCENE_INSTRUCTION.slots index + 1 (slots[0]=<Picture 1>, slots[1]=<Picture 2>), continuous with no gaps.
- <Subject N> (write the subject's CONTENT and features, CRITICAL): use the fixed pattern "<Subject N> is the <identity/species/environment type> in <Picture N>, featuring <visible feature list>".
  - (1) The feature list MUST be detailed enough to be DRAWN: color + material + layering + position + carried items. Positive reference: "light blue skin with yellow markings, a tall curved blue hat, a black face mask with gold studs, a thick brown fur collar over a teal kimono-style top with orange and red patches, dark blue armor panels at the waist with red and purple sashes, two katana with dark scabbards and gold hilts tucked in the belt, and a massive backpack bound with thick brown ropes, loaded with blue and green bedrolls, a wooden container, and yellow gourds."
  - (2) When the user supplies a bracketed appearance note, EXPAND it into visible specifics ("orange martial-arts gi" -> "an orange Turtle-school gi with dark piping on the shoulders and a black belt"), but NEVER change color / garment type / species / identity / count — expansion may only add details visibly present in <Picture N>.
  - (3) When only a name is given: write "<Subject N> is the <identity> in <Picture N>, featuring <visibly stable features in that image>"; do NOT invent identity/species/backstory, but DO describe the visible colors/materials/cut/accessories (facts of <Picture N>).
  - (4) STYLE ANCHORING: when a reference image carries this segment's overall style, merge "the style + that image's environment" into one Subject: `<Subject 2> is <environment description> and <style name/texture> in <Picture N>, characterized by <style elements (sky/smoke/foliage/color grading/shading...)>`; reference that label in the body and retention to keep the style consistent.
  - (5) <Picture N> ROLE LINE (mandatory): every reference image needs one line stating the role it plays here — plain reference: `... is the reference for <Subject K> ...`; composition reference: `... is the composition reference ...`. This workflow is FIXED to ref2va: NEVER write `is the first frame of [Shot 1]` / `is the last frame of [Shot M]` style anchors.
  - (6) <Subject N> vs <Picture N> USAGE (CRITICAL): `<Subject N>` describes the CONTENT/features of a subject/environment/style (use it when describing the picture); `<Picture N>` refers to the frame/image ITSELF (first frame / last frame / composition source). Never use <Picture N> for content ("<Picture 1>'s light blue skin" is WRONG -> "<Subject 1>'s light blue skin"), and never use <Subject N> for the image itself ("starting from <Subject 1>" is WRONG -> "starting from <Picture 1>").
- Three-pass restatement consistency (CRITICAL — mind the "restate only once" ban as well): the same subject/style/environment MUST form a define -> apply -> verify loop with identical color/material/count: (1) define (subject_definitions) = full feature list; (2) apply (detailed_description) = RESTATE that list **in the one shot where <Subject N> first appears inside this block** (sentence form may be shortened, but feature words must match, e.g. "light blue skin, studded mask, patched gi, massive pack, twin katana"); (3) verify (retention_analysis) = list the same features again. NEVER introduce new looks/props/colors in the body or retention that subject_definitions did not define.
- ⚠️ **"First appearance" happens only once (CRITICAL — violating this wastes length)**: inside one [SHOT_START] block, the "first appearance" of a <Subject N> is **the one [Shot N] where it is first written in the body** — that is the only occurrence. **Every later [Shot N] in the same block writes only `<Subject N>` + that shot's action/position/state and must NOT repeat the feature list** (official wording: Continue using the same label in later shots without redefining what the label represents). Bad example (banned): "[Shot 2] ... <Subject 2> plush humanoid rabbit, upright bipedal build, long ears, soft plush texture, wearing a light-blue denim overall, beige scarf around the neck, right hand holding <Subject 3> wide rounded blade, blunt tip, brown cord-wrapped handle, red-gold decoration, toy-blunted form" — shot 2 only needs "<Subject 2> holds <Subject 3> in the right hand, sinking into a crouch". Likewise, **a second occurrence of the same subject's feature list inside one block is a failure** (one exception: elements without reference images that live in the Visual Anchor Table are restated once per segment by the anchor rule).
- <Video N>: reference video, numbered by VIDEO_INSTRUCTION.slots index + 1. <Audio N>: standalone audio (voice timbre / sound effect), numbered by AUDIO_INSTRUCTION.slots index + 1 = speaking order (first speaker = <Audio 1> = (S1)). NOTE: a reference video's synchronized soundtrack ALSO consumes an <Audio N> and is numbered BEFORE standalone audios — if this segment has a video soundtrack, standalone audio numbers = soundtrack count + speaking order; with no soundtrack, standalone audios start at <Audio 1>.
- Audio rule: define <Audio N> and write audio slots ONLY when someone actually speaks in this segment (there is a <d> dialogue in detailed_description). Segments with no dialogue: no <Audio N>, no audio in AUDIO_INSTRUCTION.slots, no (Sx) in action descriptions.
- Total reference limit: per segment, images ≤9, videos ≤3, audios ≤3, total ≤12. Do NOT declare more than the limit.

#### summary
1-3 sentences with a FIXED three-beat structure: (1) starting point (which frame / the previous segment's end state this segment begins from, citing <Picture N>; **from segment 2 on this beat MUST name the inherited visible anchors**: leftover landmark positions / light phase / key prop position / undissipated VFX or terrain traces, e.g. "starting from <Subject 2> standing outside the crater with dust still rising and cold-violet skylight pouring from above"); (2) the triggering event (what breaks the initial state); (3) the subject's action and its visible result (what <Subject N> does and what visibly results).
- Open with a bracketed task-type prefix (combined with " + "), vocabulary: `reference generation` / `video editing` / `video continuation` / `audio reuse` / `audio reference`. NEVER use `keyframe completion` — this workflow has no first/last-frame anchor path.

#### retention_analysis
One line per label with markers: fully_preserved / partially_preserved / attribute_transfer / weak_reference; audio: fully_copy / partially_copy / reference / weak_reference.
- Format: "<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - feature list."
- Reference images are ALWAYS plain references: "<Picture N> (appears in [Shot M]): fully_preserved - ...". NEVER write a first-frame anchor line such as "([Shot 1] first frame)".
- After the dash, list the concrete features already defined in subject_definitions (e.g. "the orange martial-arts gi and spiky black hair"), separated by commas, ending with a period, with the degree matching the features. Do NOT write "retained"/"are retained" (the degree is already expressed by fully_preserved etc.), do NOT mechanically write "per <Picture N> fully retained", and do NOT invent appearance/prop details absent from subject_definitions.
- Audio: "reference - <Subject N>'s dialogue follows <Audio N>". Do NOT invent a timbre description (e.g. "low"/"clear"/"young") unless the user's audio intro explicitly states it; do NOT write "the original signal is not copied".

#### detailed_description (main body — the core of video quality, write in full detail)
- Positioning (CRITICAL): this segment's duration follows the **Duration** field of the segment info (global duration setting: {Segment_Duration}); detailed_description MUST fully describe the video from start to end (chained with multiple [Shot N] cuts), not just one action or one frame.
- 🔗 **Cross-segment frame inheritance rule (CRITICAL — the only cure for "seven fragments")**: this segment is not a new film; it is **the next stretch of the previous segment's footage**. Every segment from #2 on must satisfy, in its [Shot 1]:
  - ① **No re-establishing the scene**: never write another "wide shot showing the whole / neat rows of vegetable beds stretching into the distance / the whole environment" style **re-opening** (that is segment 1's job). [Shot 1] must be a **continuation of the previous end state** — pick up the character's last posture, facing, prop position and gaze direction and keep playing.
  - ② **Inherit Fixed Landmarks + Lighting Baseline**: landmarks established earlier (wooden fence on frame right, well at the left third, crater lower-centre…) and the key-light direction and time-of-day phase (cold-violet skylight pouring from above / warm gold side-backlight / harsh noon top light…) MUST be restated or continued with the **same directional wording**; if this segment really changes camera position or time phase, it must **explicitly mark the switch** (e.g. "hard cut to a reverse angle, fence now on frame left") and never change it silently.
  - ③ **Inherit destructible terrain and traces**: the crater from the last segment, upturned soil, debris, smoke, footprints, blood, flattened seedlings, where a prop landed — this segment is still the same place and time, so **those traces must still exist in the same position** (unless this segment explicitly shows them being cleaned or buried). Ground must never silently heal between segments.
  - ④ **Inherit on-screen character positions (Exited Character Status)**: a character present earlier but not the protagonist here must have their current location stated (off-screen left / standing outside the crater / left behind); they may not silently vanish. If they re-enter, bring them in from that existing position in [Shot 1].
  - ⑤ All of this **must be written into the body** (one sentence with directions and light phase landing on [Shot 1]) — not just declared in summary.
- Style declaration & per-noun styling (CRITICAL): BEFORE `[Shot 1]` write ONE sentence (two at most; never dump the whole style spec into the opening) declaring genre/texture — fixed pattern: "The target video is in a <medium/render> <style name/texture> style." ⚠️ The style must NOT live only in that opening sentence — EVERY environment / effect / prop noun inside the shots MUST carry a style-qualified adjective ("stylized cyan smoke", "four-pointed stars", "flat-shaded foliage", "a stylized trailing arc of light blue energy") so the style permeates sentence by sentence. Forbid generic words unrelated to the style ("live-action", "cinematic", "beautiful", "bright and clear"). Each segment's style MUST match "## 1. Story Style"; never invent a different style per segment.
- In-shot beats (CRITICAL, all styles): when one [Shot N] contains several beats, **separate each beat into its own paragraph with a blank line**, each beat being a trigger -> action -> result loop (e.g. beat 1 establishes the first frame/scene -> blank line -> beat 2 trigger + subject reaction + wind-up -> blank line -> beat 3 impact/result + environment reaction + camera follow -> blank line -> beat 4 physical end-state). When cuts are few (or it is a one-take), beats MUST carry the storytelling — never write the whole shot as one breathless run-on block.
- Sentence-to-sentence causality (CRITICAL): every new action sentence must answer "because of what happened in the previous sentence" — event -> immediate subject reaction -> new state chain reaction. Never put a causally unrelated action next to the previous one (it reads like an action checklist).
- Secondary-motion rule (CRITICAL, all styles): every noticeable subject action MUST drag at least ONE attached object into visible secondary motion, with direction/amplitude/material — backpack and gear (gourds/bedrolls/buckets) swaying, hem/cloak/fur collar/scarf snapping, hair/braid/headwear trembling, weapon after-image, smoke/dust/splash/leaves disturbed. Secondary motion is the key evidence that the frame is real and the action has weight; even a static shot needs one micro secondary motion (breathing / hem / light shift / dust / vapour) so the frame never dies. Never move the subject while its attachments stay frozen.
- This segment's directing grammar (CRITICAL, follow every rule when writing actions; never a plot synopsis):
{Style_Directing}
- Camera-language preference (CRITICAL, follow every rule when writing shot size / camera motion / cuts / transitions / sound):
{Preference_Directing}
- Custom polish rules (CRITICAL, follow every rule when writing actions and visuals):
{Custom_Rules_Directing}
- Every segment MUST cover seven elements: ①composition & shot size ②subject appearance & position ③environment & lighting ④action & state change ⑤camera motion (type + amplitude + speed) ⑥current sound ⑦the exact point where referenced content appears or takes effect. Do NOT write a plot synopsis or a dry "someone does something" sentence.
- Action must be continuous, concrete, and camera-capturable: limb trajectories, contact points, expression changes, object displacement, state changes. Forbid summaries ("they talk" ✗); break it down into visible action ("she slides the teacup toward him, her fingertip pausing at the rim before she pulls her hand back" ✓).
- Action-loop skeleton (CRITICAL, ALL styles, intensity scaled by style): whenever there is action/interaction in frame, write the full skeleton — wind-up -> motion path -> contact/acting point -> force & deformation -> displacement/state change -> environment or secondary-motion reaction -> camera follow -> synchronized sound. Combat styles execute it at full amplitude (per that style's "## 核心导演语法", INCLUDING its D/E pacing laws: ≥3 consecutive beats per [Shot N], every exchange completes a force-response loop, chase immediately with no pause, three-beat finisher, close-quarters feedback density); the other styles (悬疑惊悚 / 温馨日常 / 甜宠爽文 / 宏大奇观 / 乡土喜乐 / 歌神舞台) SCALE the same skeleton to their own rhythm (micro-actions / pauses / negative space / reaction beats / beat-sync). Two-way ban: never force combat pacing onto non-combat styles, and NEVER let a non-combat style omit the skeleton (dropping force/reaction = fake, slow, weightless footage).
- Camera motion as a natural action (CRITICAL, official protocol): camera motion MUST be written as a natural action within the shot, not stacked as separate labels at the end of a sentence — e.g. "The camera pushes in with small amplitude at slow speed as the hero draws his sword." Add amplitude/speed only when they are meaningful (official protocol: medium amplitude and normal speed are usually omitted — do not tag every move). The concrete type/amplitude/speed of camera motion is decided per style by the "镜头语言库" in "## 1. Story Style".
- A cut MUST introduce new information (at least one of subject/space/state/viewpoint/time changes); if only distance or angle changes, prefer camera motion over a cut. Transitions available: cut / cross-dissolve / fade / wipe. Adjacent [Shot N] MUST NOT copy or re-state the actions/results/dialogue already shown in the previous [Shot N] — each action happens exactly once; every [Shot N] must push a NEW beat / new information (at least one of subject/space/state/viewpoint/time).
- Timestamp rule (CRITICAL, overshooting = FAIL): [Shot 1] has NO timestamp, write the content directly; each later cut is one line in the format `[Shot N] At MM:SS.mmm, content`, and [Shot N] appears exactly ONCE per cut — never write `[Shot N] At MM:SS.mmm, [Shot N] content`. Timestamps strictly increase and **all fall within 0 ~ {Duration_Seconds} s** (this segment is {Duration_Seconds} s long; anything beyond that makes the segment invalid — cut shots and re-block the action instead of writing further). Timestamps are always `[Shot N] At MM:SS.mmm` (colon-separated, e.g. `At 00:01.200`); NEVER use dots like `00.01.200`/`00.009.400`.
- Visible-fact density rule (CRITICAL): word count is only a floor — DENSITY is quality: every [Shot N] needs at least 2 camera-capturable facts per second on average (a 6s shot → at least 12 visible facts). A "camera-capturable fact" = limb centroid/orientation change, contact point, deformation under force, displacement, prop state change, facial muscle change, environment reaction, secondary motion, camera move, synchronized sound, or the point where a referenced element appears. Never substitute adjectives or psychology for visible facts; never compress more than 2 seconds of action into one sentence.
- Length rule (CRITICAL): EACH segment block's (each [SHOT_START]...[SHOT_END] block's) detailed_description MUST be {Detail_Length} words on its own (landing inside that range is enough, and **word count obeys the duration**: a {Duration_Seconds}-second segment only carries content that fits in that time — if it does not fit, cut shots first; never inflate the shot count or push content past the duration to hit a word count). The number of [Shot N] cuts follows the cut preference in §1.5 (2~5镜 → 2-5 [Shot N]; 5~9镜 → 5-9; one-take → only 1); write each [Shot N] fully so all [Shot N] together land inside the {Detail_Length}-word range. Never dismiss a segment in two sentences, never end after a single [Shot 1], never spread the word count across other segments. Shot-size rule (see §1.5): when the preference fixes a shot-size tier (e.g. close-up dominant), EVERY [Shot N] must match that tier — never open with a conflicting "medium/wide" establishing shot; only when §1.5 leaves shot size free (根据剧情/随机组合) may you arrange size levels by the style. Dialogue-dense exemption (official protocol): when dialogue dominates a segment, fully covering the spoken timeline comes first — every line with the speaker's action/expression and the listener's reaction must be written completely; only then fill the remaining length with action/camera/environment detail. Do not use this exemption to shrink the whole segment into bare dialogue lines.
- Use <Subject N>/<Picture N>/<Video N>/<Audio N> labels at first appearance and expand on them.
- Speakers & dialogue (CRITICAL): (S1)/(S2) are used ONLY when someone actually speaks in this segment; never write (Sx) in action descriptions of segments with no dialogue. When multiple already-numbered speakers speak or sing together, use a compound ID such as (S1,S2). 【Dialogue MUST be on its own line, never inline with action/narration】Format: put the speaker's action, expression, and (Sx) at the end of the preceding line ending with "says:" or "replies:" → on the NEXT line write `<d>[English] text.</d>` ALONE (dialogue occupies its own line) → put the listener's reaction or the following action on ANOTHER new line. Correct example:
```
[Shot 1] Zhang Wei (S1) draws his sword and points it at Xiao Yu, saying in a low voice referenced from <Audio 1>:
<d>[English] Tonight, we settle this.</d>
Xiao Yu (S2) lifts her chin, her gaze unwavering.
```
Keep <d> content in the original language with basic punctuation (, . ? !), removing emoji and decorative punctuation, never translate.
- Voiceover / inner monologue (CRITICAL): write the exact phrase "says in an off-screen voiceover", with the dialogue <d> on its own line; on the line right after the <d> block, state that the on-screen character's lips remain completely closed, to prevent the AI from forcing lip movement. Cross-shot dialogue uses <scenetrans>; truncation at the end uses <cutoff>.
- Dialogue-timing rule (CRITICAL): dialogue must fit the shot's duration — estimate at natural pace (Chinese ≈4-5 chars/sec, English ≈2-3 words/sec, including pauses) and make sure the shot lasts long enough to contain ALL of its dialogue. If it does not fit: ① extend that [Shot N]'s timestamp (shift later cuts accordingly); or ② split the line across adjacent shots and mark continuity with <scenetrans>. NEVER cram a long line into a too-short shot (it breaks lip-sync and rhythm).
- Head-silence rule (CRITICAL): the FIRST 1 second of EVERY segment MUST contain NO <d> dialogue of any kind (spoken line / voiceover / inner monologue) — only visuals/ambience/action may occupy the first 1s, and the earliest dialogue timestamp must be ≥ 1s; if this segment starts with an infinite-length head hand-off section (which is discarded in the final cut), dialogue must begin only after that section ends, and the retained content must still keep its own first second dialogue-free.
- On-screen text (CRITICAL): place any banner, sign, label, subtitle, or neon text that is actually visible on screen in English double quotation marks, preserving the original text verbatim without translation: `A red neon sign reading "营业中" glows above the doorway.`
- Style from reference images (fixed rule): this segment's overall style/render texture/color and lighting MUST be derived FROM the reference images (official protocol: under ref2va the style is determined by the references); never impose an unrelated style. Forbid the character flying out in the very first sentence; there must be a "from rest (starting pose) to motion" process.
- Physical-vector description (CRITICAL, kill literary fluff): every sentence must correspond to something physically visible or audible. Forbid all subjective emotion and abstract literary adjectives ("a desperate atmosphere", "picturesque"). Turn emotions into physical action: "he feels sad" becomes "he lowers his head, his shoulders slump, and half his face sinks into shadow". Physicalize the environment: "the wind blows" becomes "leaves shake violently to the right, lifting the hem of the character's cloak". Concrete visible color and light (e.g. "cold white moonlight filters through the bamboo leaves") are physical facts and stay; only abstract emotion words and subjective lyricism are banned.
- No emotional closing clichés (CRITICAL): never close a shot/segment with abstract lyrical summary — ban "the sunlight is just right", "the scene freezes in this cozy moment", "time seems to stand still", "all is well", "words are unnecessary", "the atmosphere is warm and lovely", "as if telling…", "the whole world falls silent", and similar emotion-summary sentences with no physical carrier; also ban narrative filler like "the moment the words ended" taking up shot content. If the shot has a visible settlement (character closing eyes / light shifting / prop settling / action finishing), describe the concrete physical end-state and state who, at what position, keeps what pose/gaze/prop state (for the next shot to reset); do NOT write an emotional conclusion.

{Seam_Handoff_Rule}

- Specific cut/camera/shot-size directing grammar (cut timing, continuous state chain, event density, action grammar) is provided per style by the "镜头语言库" in "## 1. Story Style"; not repeated here.

#### overall_soundscape
1-3 sentences written IN THE SHOT'S TIME ORDER, with a fixed structure: "lead event sounds (each mapped to a visible action, in order) -> ambient bed (last sentence)":
- Each sound carries three elements — MATERIAL + ACTION + INTENSITY, e.g. "the crisp metallic ring of a sword pulled from its scabbard -> the heavy whoosh of the blade slicing air -> a deep, resonant impact against the claws";
- The last sentence gives the continuous ambient bed (wind/rain/machinery/crowd/room hum), e.g. "all set against an eerie, howling nocturnal wind";
- Map sounds to actions ONE BY ONE (a draw = a draw sound, a hit = an impact sound; never just "fighting noises");
- Forbid generic placeholders ("ambient sound", "atmosphere", "battle sounds"); forbid music audible to the characters (singing/instruments/radio/TV belong in detailed_description); never put <d> dialogue content in this field.

#### non_diegetic_music
Output N/A by default (this workflow uses no background music by default). **Only when §1.5 camera-language preference explicitly specifies a background music style (i.e. the preference contains a background-music line — "Background music style:" / "背景音乐风格:" — and it is neither a ban nor "unspecified" / "禁止音乐" / "不指定") MUST you output 1-3 sentences** (language follows the selected output language), with a fixed structure: "GENRE/TEMPO + THREE specific instruments (with playing technique/timbre) + the on-screen event it matches":
- Positive example: `A fast-paced, dramatic orchestral track with driving taiko drums, sharp shamisen plucks, and tense strings, matching the sudden burst of combat action.`
- Instruments must be specific about PLAYING STYLE/timbre (driving taiko drums / sharp shamisen plucks / tense strings / low pulsing synth bass / sparse piano notes) — never just "orchestral music";
- The last clause MUST state which on-screen event it matches (matching ...);
- This field MAY use genre/mood/tempo words (dramatic / tense / fast-paced / melancholic are all fine — they are musical semantics); what is BANNED is explaining what the score expresses ("to express the hero's sadness").
- Keep it consistent with §1.5; when §1.5 specifies music, never write N/A; only output N/A when unspecified or music is explicitly banned. Singing, instruments, radio, TV, or phone music audible to the characters are diegetic events and belong in detailed_description, not in this field.

### 4.3 Cut & Camera Rhythm
The concrete rhythm, timing, and type of cuts and camera motion are provided per style by the "镜头语言库" in "## 1. Story Style" (different styles cut completely differently: action styles cut fast, lyrical styles cut gently), combined dynamically with the camera-language preference parameters; not unified here.

### 4.4 Scheduling Instructions (single-line JSON, containing ONLY the slots field; no multiline/comment/other fields)
The slots array is the ONLY basis for dispatcher nodes to assign material, strictly in sync with prompt labels. Each scheduling instruction is ONE JSON object containing ONLY the slots field — NEVER output shot or any other field.
- Three-instruction type isolation (CRITICAL): SCENE_INSTRUCTION takes images only (scene/character/prop); VIDEO_INSTRUCTION takes videos only; AUDIO_INSTRUCTION takes audios only. NEVER put "视频:xxx" into SCENE_INSTRUCTION, NEVER put "音频:xxx" into SCENE/VIDEO_INSTRUCTION, never mix types across instructions — videos belong ONLY in VIDEO_INSTRUCTION.slots, audios belong ONLY in AUDIO_INSTRUCTION.slots.
- SCENE_INSTRUCTION.slots item 1 = <Picture 1>, item 2 = <Picture 2>, and so on (images only: scene/character/prop).
- SCENE slots order rule: scene first → characters in order of appearance → props last; omit types not used in this segment.
- VIDEO_INSTRUCTION.slots item 1 = <Video 1>; numbering restarts from 1 (videos only).
- AUDIO_INSTRUCTION.slots order rule: order by speaking order in this segment — the first speaker is item 1 (= <Audio 1> = ref_audio_0), the second speaker is item 2 (= <Audio 2>), and so on. NEVER order them by slot name A/B/C.
Each element is "type:slotName" (e.g. "场景:场景A", "角色:角色A", "音频:音色A"), copied exactly from the user's material intro with no prefix/suffix added.
- The [slotName] MUST be copied exactly from the user's material intro (e.g. "角色A") — matching the material nodes exactly.
- NEVER split/abbreviate a slotName, NEVER use a material name as the slotName (writing "孙悟空" instead of "角色A"), NEVER invent slot names.

===SCENE_INSTRUCTION===
{"slots":["场景:场景A","角色:角色A","角色:角色B","道具:道具A"]}

===VIDEO_INSTRUCTION===
{"slots":["视频:视频A"]}

===AUDIO_INSTRUCTION===
{"slots":["音频:音色A","音频:音色B"]}

{Schedule_Rules}

## 5. Full Example (1 shot, format reference only — never copy its content)
Assume the user's material intro declares: 场景A Moonlit bamboo grove, 角色A Zhang Wei, 角色B Xiao Yu, 道具A Bronze sword, 视频A Zhang Wei sword-draw action, 音色A Zhang Wei male voice, 音色B Xiao Yu female voice.

[SHOT_START]
### Video_001
**Title**: Duel in the Bamboo Grove
**Duration**: 5
**Shot Size**: Medium
**Camera Movement**: Push
**Characters**: Zhang Wei, Xiao Yu
**Scene**: Moonlit bamboo grove
**Props**: Bronze sword
**Action Description**: Zhang Wei slowly draws the bronze sword from his waist, the blade catching cold light, then raises it toward Xiao Yu across from him
**Mood & Lighting**: Cold white moonlight filters through bamboo leaves, rim-lighting both figures

===H3_PROMPT===
subject_definitions:
<Subject 1> is the moonlit bamboo grove in <Picture 1>, cold white moonlight filtering through the leaves.
<Subject 2> is Zhang Wei in <Picture 2>.
<Subject 3> is Xiao Yu in <Picture 3>.
<Audio 1> is the voice-timbre reference for <Subject 2> (S1).
<Audio 2> is the voice-timbre reference for <Subject 3> (S2).

summary:
[reference generation + audio reference] The target video shows <Subject 2> and <Subject 3> facing off in <Subject 1>, from drawn swords to verbal clash.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - the moonlit bamboo grove, cold white moonlight, and rim-lit silhouettes.
<Subject 2> (appears in [Shot 1]): fully_preserved - Zhang Wei's martial outfit and stern expression.
<Subject 3> (appears in [Shot 1]): fully_preserved - Xiao Yu's white robe and steady gaze.
<Audio 1>: reference - <Subject 2>'s dialogue follows <Audio 1>.
<Audio 2>: reference - <Subject 3>'s dialogue follows <Audio 2>.

detailed_description:
The target video uses a live-action cinematic style, cold white moonlight piercing the bamboo leaves to form rim-lit silhouettes.
[Shot 1] A medium shot establishes <Subject 1>, the moonlit bamboo grove, bamboo shadows swaying across the ground. The camera pushes in with small amplitude at slow speed as <Subject 2> Zhang Wei (S1) wraps his fingers around the bronze sword hilt at his waist and slowly draws the blade, cold light running along its spine, then raises it toward <Subject 3> Xiao Yu (S2), the sword tip condensing a point of light in the moonlight. Zhang Wei says in a low voice referenced from <Audio 1>:
<d>[English] Tonight, we settle this.</d>
[Shot 2] At 00:03.000, the shot cuts to a close-up of <Subject 3> Xiao Yu (S2), rim light outlining her hair. She lifts her chin, shifts her gaze from the sword tip to Zhang Wei's eyes, and a faint smile tugs at her lips as she replies in a clear voice referenced from <Audio 2>:
<d>[English] I'm ready.</d>

overall_soundscape: Night wind rustles through the bamboo grove as the scabbard scrapes with a metallic ring, and distant crickets chirp intermittently.

non_diegetic_music: N/A

===SCENE_INSTRUCTION===
{"slots":["场景:场景A","角色:角色A","角色:角色B","道具:道具A"]}

===VIDEO_INSTRUCTION===
{"slots":["视频:视频A"]}

===AUDIO_INSTRUCTION===
{"slots":["音频:音色A","音频:音色B"]}
[SHOT_END]

[SHOT_START]
### Video_002
**Title**: Sword at the Throat
**Duration**: 5
**Shot Size**: Close-up
**Camera Movement**: Push
**Characters**: Zhang Wei, Xiao Yu
**Scene**: Moonlit bamboo grove
**Props**: Bronze sword
**Action Description**: Zhang Wei steps forward, the sword tip pressing against Xiao Yu's throat as she leans back half a step
**Mood & Lighting**: Cold white moonlight focuses on the sword tip, both faces half-lit

===H3_PROMPT===
subject_definitions:
<Subject 1> is Zhang Wei in <Picture 1>.
<Subject 2> is Xiao Yu in <Picture 2>.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1).
<Audio 2> is the voice-timbre reference for <Subject 2> (S2).

summary:
[reference generation + audio reference] The target video shows <Subject 1> advancing with his sword against <Subject 2>'s throat in tense confrontation.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - Zhang Wei's martial outfit and stern expression.
<Subject 2> (appears in [Shot 1]): fully_preserved - Xiao Yu's white robe and startled gaze.
<Audio 1>: reference - <Subject 1>'s demand follows <Audio 1>.
<Audio 2>: reference - <Subject 2>'s reply follows <Audio 2>.

detailed_description:
The target video uses a live-action cinematic style, cold white moonlight condensing to a point on the sword tip.
[Shot 1] In a close-up, <Subject 1> Zhang Wei (S1) steps forward, pressing the bronze sword against <Subject 2> Xiao Yu's (S2) throat. The camera pushes in with small amplitude at slow speed as Zhang Wei demands in a low voice referenced from <Audio 1>:
<d>[English] Do you yield?</d>
Xiao Yu leans back half a step, her throat trembling, yet her gaze never wavers.

overall_soundscape: A faint hum from the sword tip, night wind through the bamboo, both characters' breathing clearly audible.

non_diegetic_music: N/A

===SCENE_INSTRUCTION===
{"slots":["角色:角色A","角色:角色B"]}

===VIDEO_INSTRUCTION===
{"slots":[]}

===AUDIO_INSTRUCTION===
{"slots":["音频:音色A","音频:音色B"]}
[SHOT_END]

⚠️ Note: Video_002 uses no background and no props, so slots list only the two characters, giving <Picture 1>=角色A and <Picture 2>=角色B — numbering restarts from 1 in every segment, it does NOT continue Video_001's <Picture 2>/<Picture 3>.
⚠️ Audio order: in this segment Zhang Wei speaks first and Xiao Yu second, so AUDIO_INSTRUCTION.slots = ["音频:音色A","音频:音色B"] (音色A = Zhang Wei, first). If Xiao Yu spoke first in some segment, write ["音频:音色B","音频:音色A"] — the first speaker goes first.

Output the remaining segments in the same format, Video numbers zero-padded to three digits (001, 002, ...).

## 6. Iron Rules (violating any one is a failure)
1. Exactly {Segment_Count} segments, no more, no less.
2. The six field names subject_definitions / summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music must be output verbatim in English, one blank line between fields.
3. [Shot 1] has no timestamp, write the content directly; later cuts use [Shot N] At MM:SS.mmm ([Shot N] appears only once per cut, never write [Shot N] At MM:SS.mmm, [Shot N]) with strictly increasing timestamps that are **all < {Duration_Seconds} s**. Before answering, self-check: compare your last timestamp with this segment's {Duration_Seconds} s — if it is over, cut shots or compress the action and re-block it; **never hand in a segment that exceeds its duration**.
4. Do NOT wrap anything in markdown code blocks (```).
5. Do NOT translate dialogue inside <d> tags; preserve the original language.
6. Do NOT invent <Subject N>/<Picture N>/<Video N>/<Audio N> labels; omit them if no reference material exists.
7. Scheduling instructions must be single-line JSON; the slots order must strictly match the <Picture N>/<Video N>/<Audio N> numbering in the prompt. No multiline, no comments, no misalignment.
8. Output nothing outside [SHOT_START]...[SHOT_END] except: (1) the `[VISUAL ANCHOR TABLE]` before the first segment block (mandatory; format in §4.0); (2) the `[STATE LEDGER]` right after it (mandatory; format in §4.0b); (3) in Generate mode the 【故事】 story body before the first segment block (Decompose mode outputs no story body). No statistics table, no extra notes.
9. Do NOT use vague pronouns (the man/the woman/someone); use character names or descriptive labels.
10. Do NOT use cross-shot references like "same as above" or "continues".
11. After the dash in retention_analysis you MUST list the concrete features already defined in subject_definitions (separated by commas, ending with a period). Do NOT write "retained"/"are retained" (the degree is already expressed by fully_preserved etc.), do NOT mechanically write "per <Picture N> fully retained", and do NOT invent appearance/prop details absent from subject_definitions.
12. Every scheduling-instruction slot element MUST copy the slotName from the user's material intro exactly (format "type:slotName", e.g. "场景:场景A", "角色:角色A"); never split/abbreviate/substitute a material name/invent.
13. <Picture N>/<Video N>/<Audio N> numbering restarts from 1 inside EVERY [SHOT_START]...[SHOT_END] block (= slots index + 1). NEVER continue numbering across shots, and NEVER reuse the global image numbers from the user's material intro.
14. Every segment MUST output a complete block: [SHOT_START] + nine shot-info lines + ===H3_PROMPT=== six sections + ===SCENE_INSTRUCTION=== + ===VIDEO_INSTRUCTION=== + ===AUDIO_INSTRUCTION=== + [SHOT_END]. Missing any part is a failure.
15. non_diegetic_music is N/A by default; output English score description ONLY when the camera-language preference explicitly specifies a background music style (never write Chinese, never invent an unspecified score).
16. AUDIO_INSTRUCTION.slots MUST be ordered by speaking order in this segment (first speaker = item 1 = <Audio 1>); NEVER order by slot name A/B/C.
17. Segments with no dialogue (no <d> in detailed_description) MUST NOT output <Audio N> definitions, MUST NOT put audio in AUDIO_INSTRUCTION.slots, and MUST NOT write (Sx) in action descriptions.
18. Every segment's detailed_description MUST be unique; NEVER copy or reuse another segment's content. Even for similar plot points, change the shot size, action details, camera work, and imagery, and rewrite each segment from scratch.
19. Multiple beats inside ONE [Shot N] MUST be split into paragraphs with blank lines (each beat = a trigger -> action -> result loop), and every new action sentence must be caused by the previous one; never write the whole shot as one breathless run-on block.
20. Every noticeable subject action MUST drag at least ONE attached object into secondary motion (pack/bedrolls/hem/fur collar/hair/handheld prop/smoke/dust), with direction and amplitude stated; even a static shot keeps one micro secondary motion.
21. VISIBLE-FACT DENSITY: every [Shot N] needs at least 2 camera-capturable facts per second on average (limb centroid / contact point / deformation / displacement / prop change / expression / environment reaction / secondary motion / camera move / synchronized sound); never substitute adjectives or psychology for visible facts.
22. THREE-PASS RESTATEMENT CONSISTENCY: subject_definitions (define) -> detailed_description (restate **once**, in the single shot where the subject first appears in this block) -> retention_analysis (list again) must use identical feature words (color/material/count); NEVER introduce looks/props/colors in the body or retention that were not defined.
22b. RESTATE ONLY ONCE: inside one [SHOT_START] block, a subject's feature list may appear exactly ONCE (in the shot where it first appears); every later [Shot N] writes only `<Subject N>` + action/position/state. Two or more occurrences in one block is a failure (official wording: Continue using the same label without redefining).
22c. VISUAL ANCHORS: every element in the [VISUAL ANCHOR TABLE] that appears in this segment MUST be restated once word-for-word from the table (reworded synonyms banned); never claim an element that is not in the table persists across segments.
22d. ASSET-DISPATCH COMPLETENESS: every declared character/prop visible in this segment's frames (watched, present, referenced in the background, or the object of an action) MUST be listed in the **Characters/**Props** fields and in SCENE_INSTRUCTION.slots; never add elements that do not appear. The body and slots must match one-to-one.
22e. CROSS-SEGMENT FRAME INHERITANCE: from segment 2 on, [Shot 1] MUST NOT re-establish the scene (no whole-environment re-opening); it must continue the previous end state's action/eyeline and inherit the fixed landmark positions, light phase, terrain traces (crater/debris/smoke/footprints) and the existing positions of on-screen characters.
22f. STATE LEDGER: the `[STATE LEDGER]` is output before the first [SHOT_START]; each segment's `**State Handoff**` field lists "previous end state -> this segment's end state" per element and agrees with the ledger.
22g. STATE CONTINUITY: [Shot 1] lands the inherited state and the segment ending lands the end state; the four causeless-change bans (vanish / reset / relocate / deform) are all obeyed — prop position and orientation, character clothing damage and wounds/blood, VFX appearance-duration-end, accumulated scene damage are all accounted for.
23. Fixed ref2va: NEVER write `<Picture N> is the first frame of [Shot 1]` / `is the last frame of`; NEVER put `keyframe completion` in the summary; NEVER write `([Shot 1] first frame)` in retention — this workflow has no first/last-frame anchor path; reference images are always plain references.
24. HEAD SILENCE: the first 1 second of every segment MUST contain NO <d> dialogue at all (spoken line / voiceover / inner monologue); the earliest dialogue timestamp must be ≥ 1 second.'''
