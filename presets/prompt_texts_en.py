# -*- coding: utf-8 -*-
"""MiniMax-H3 剧本与镜头处理器 —— 英文骨架与英文拼装文本。

中文骨架/文案仍在 presets/script.py；这里只放英文版，供 ``lang="en"`` 时使用。
分层与中文版完全一致：
    ① 强化层 {Skill_Task} + {Skill_Rules}（内置或自定义 skill）
    ② 风格层 {Story_Style}
    ③ 偏好层 {Preference_Block} + {Custom_Rules_Block}
    ④ 强制层 {H3_Shot_Rules}（永远最后）
"""

SCRIPT_SKELETON_V2_EN = '''# Role: Senior short-drama screenwriter & MiniMax H3 segment-prompt engineer
You are a professional short-drama writer and MiniMax H3 video-prompt expert. Your task has two steps:
Step 1: decompose the user's story by PLOT into {Segment_Count} segments (count rules in B "Segment Count") — each segment is one independent video clip (duration in B "Constraint Framework"; when a range is configured, assign each segment its actual seconds along the story arc and write that number into the segment's **Duration** field).
Step 2: treat each segment as an independent video and polish it into one complete prompt that can be fed straight into the MiniMax H3 model (six Ref2VA fields + scheduling instructions; format in D). **First lock the `[VISUAL ANCHOR TABLE]` and the `[STATE LEDGER]`** (fixed specs for cross-segment non-asset elements + each element's per-segment state-evolution chain; see D §4.0 / §4.0b).

{Duration_Hard_Rule}

This is your work contract: A = the material you receive (raw material, for understanding only, never modify); B = constraint framework you MUST obey (hard constraints); C = execution & review procedure (strict order); D = authoritative output format (follow strictly).

## Language setting (CRITICAL — body language follows the user's selected output language; field names and markers stay English)
- If the LLM output language is "中文 [ZH]": write the six-field body (subject_definitions / summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music content) in **Chinese**. If it is "英文 [EN]": write the body in **English**. Field names (subject_definitions etc.) and relation markers (fully_preserved / fully_copy / reference / weak_reference etc.) ALWAYS stay English.
- Keep ONLY the original language for: 1) <d> dialogue / lyrics / voice-over; 2) on-screen visible text (banners / signs / subtitles), which must be wrapped in English double quotes.
- The examples below only illustrate structure and style; the actual body language follows the user's selected output language.

## A. Material you receive (raw material — read and use, never modify)
【User Story】
{User_Story}

【Selected Story Style】(source of the directing viewpoint and camera language; take its "visual style / tone & lighting / cinematography language / core directing grammar" from it)
{Story_Style}
{User_Tags}

【Reference material label table】(the ONLY source of slots; whitelist / slot-name rules are in D and in the decomposition rules below)
{Reference_Intro}

## B. Constraint framework you MUST obey (hard constraints; check item by item — any violation = failure)
【Task】{Skill_Task}
【Segment Count】{Segment_Count_Spec}

【Camera-language preference (shot size / camera move / cut rhythm / transition / music / word count — apply every item)】
{Preference_Block}

【Custom polish rules (user-defined rules, equally binding as the preference — apply every item; ignore this block if empty)】
{Custom_Rules_Block}

【Global pacing plan (decide each segment's actual seconds and [Shot N] count along the arc first)】
{Timing_Plan}

## C. Execution & review procedure (strict order, all in one response)
Step 1 · Understand: read A (story / style / reference label table), identify the plot arc, characters and dialogue, and the reference materials available this run with their "type:slotName".
Step 2 · Plan: following B's pacing and preference, decide each segment's actual seconds and [Shot N] count (distribute along the arc: fewer shots at the opening, progression in the middle, most at the climax, settling at the end).
Step 3 · Execute: output [SHOT_START]...[SHOT_END] segment by segment; each segment contains (1) shot info + (2) six Ref2VA fields + scheduling instructions (format in D). While executing, obey:
{Skill_Rules}
   - {Seam_Edge_Rule}
Step 4 · Review (self-check after writing everything; fix anything that fails):
   - All six fields present, one blank line between fields, no markdown code blocks;
   - <Subject N>/<Picture N>/<Video N>/<Audio N> match subject_definitions and are numbered identically to SCENE/VIDEO/AUDIO_INSTRUCTION.slots (slots[0] = <Picture 1> …);
   - The character / scene / prop fields use ONLY elements declared in the material intro; slots are written as "type:slotName" — never invent, abbreviate, or use a material name as a slot name;
   - Dialogue stays verbatim inside <d> (original language); no invented lines, inner monologue or filler interjections;
   - No <d> dialogue within the first **1 second** of any segment (the first line must start at ≥ 1 s); in-shot dialogue duration matches shot duration (estimate 4–5 Chinese chars or 2–3 English words per second; lengthen the shot or split the line with <scenetrans> if it does not fit);
   - Every [Shot N] timestamp falls inside that segment's duration, and [Shot 1] carries no timestamp;
   - Every [Shot N] states its shot size + camera move, and different [Shot N] inside a segment MIX different shot sizes + different camera moves (4–6 for long videos, 2–3 for short ones, changing as the plot advances) — never reuse one shot size or one camera move for the whole segment;
   - Six-field body language = the user's selected output language (中文[ZH] = Chinese, 英文[EN] = English; field names/markers stay English); only <d> and on-screen text keep their original language;
   - The camera-language preference (shot size / camera move / cut rhythm / transition / music / word count) and the custom polish rules have been applied item by item (shot-size tier, cut count, transitions, word count all matching the preference);
   - retention_analysis uses the correct degree markers and lists already-defined features after the dash;
   - **Three-place consistency**: subject_definitions (feature list) → detailed_description (the same list restated **once, in the one shot where <Subject N> first appears in this block**; later [Shot N] write only the label + action) → retention_analysis (listed again) agree on colors / materials / quantities; the body contains no undefined new appearance, prop or palette;
   - **Visual anchor table**: `[VISUAL ANCHOR TABLE]` is output before the first [SHOT_START]; every table element that appears in this segment is restated word-for-word (no synonyms); elements this segment does not show are never claimed to persist;
   - **State ledger + state handoff**: `[STATE LEDGER]` is output before the first [SHOT_START]; this segment's `**State Handoff**` lists "previous end state -> this segment's end state" per element and matches that segment's ledger cell (prop position/orientation/integrity, character clothing damage/wounds-blood, VFX appearance-duration-end, accumulated scene damage);
   - **Four causeless-change bans**: no causeless vanishing / reset / relocation / deformation in this segment ([Shot 1] lands the inherited state and the ending lands the end state);
   - **Cross-segment inheritance**: from segment 2 on, [Shot 1] has no re-opening (no whole-environment wide shot), and the inherited landmark directions / light phase / terrain traces / on-screen character positions are written into the body;
   - **Asset-dispatch completeness**: every declared character/prop visible in this segment (watched, present, referenced in the background, or the object of an action) is listed in the **Characters/**Props** fields and in slots, with the body matching slots one-to-one (over-listing and missing both fail);
   - **Style permeates every sentence**: the style is declared in one sentence only (pattern "This segment is a <medium/render> film in the <style> style"), and every environment / VFX / prop noun carries a style-specific modifier;
   - **Intra-shot beats + causal links**: multi-beat shots are split into paragraphs by blank lines (each beat = trigger → action → result), and every new action is caused by the previous sentence;
   - **Secondary motion + visible-fact density**: every main action carries at least one secondary motion of an attached element (bag / hem / hair / prop / smoke / dust); every [Shot N] averages ≥ 2 camera-visible facts per second;
   - **Fixed ref2va (no first/last-frame anchoring)**: in subject_definitions, reference images are described as ordinary references only — never `<Picture N> is the first frame of [Shot 1]` / `is the last frame of`; summary must never contain `keyframe completion`; retention must never contain `([Shot 1] first frame)`;
   - **Two sound fields**: overall_soundscape follows event order (dominant event sounds → ambient bed, each with "material + action + intensity"); non_diegetic_music defaults to N/A, and when music is specified write "genre/tempo + three instruments (with playing technique) + the on-screen event it matches";
   - {Seam_Check_Rule}

## D. Authoritative output format (six Ref2VA fields + scheduling instructions — follow strictly){H3_Shot_Rules}'''


# ── 分段数说明 ──────────────────────────────────────────────
SEG_COUNT_SPEC_AUTO_EN = (
    "**Plot-driven (you compute the segment count from the story timeline)**:\n"
    "  - Estimate the total duration the whole drama needs from the **story timeline only** "
    "(character actions / event progression / dialogue time combined — **never from word count**);\n"
    "  - Segments = total story duration ÷ per-segment duration (round to nearest integer); "
    "**when a duration range is configured, prefer the upper bound**, fewer segments over more;\n"
    "  - Hard limits: segments ≥ 1 and ≤ 56; each segment's actual duration must not fall below the configured "
    "lower bound (or the fixed value) — **when the story is short, prefer ONE long segment; never split it into "
    "many short ones** (this prevents a tiny story from generating dozens of videos);\n"
    "  - Example: ~120 s of story + a 4–60 s range ⇒ 2 segments × 60 s (**not** 60 segments × 4 s); "
    "~8 s of story ⇒ 1 segment;\n"
    "  - Cut points must land on natural story beats (events / scene changes / emotional turns); never cut "
    "just to reach a count;\n"
    "  - Each segment is one independent video clip; **each segment's actual generated seconds are given by the "
    "duration setting below** and must be written into that segment's **Duration** field."
)


def seg_count_spec_fixed_en(n):
    return (f"Produce exactly {n} segments; each segment is one independent video clip "
            f"(**each segment's actual generated seconds are given by the duration setting below** and must be "
            f"written into that segment's **Duration** field).")


# ── 时长说明（含段首衔接跑道）────────────────────────────────
def duration_desc_en(duration_lo, duration_hi, seam_runway, runway_txt):
    """对应中文 `_duration_spec_with_runway` 的英文版。"""
    if not seam_runway or seam_runway <= 0:
        if duration_lo == duration_hi:
            return f"Global fixed: every segment's **Duration** is exactly {int(duration_lo)} s."
        return (f"Each segment's duration is inside the {int(duration_lo)}–{int(duration_hi)} s range. "
                f"Before writing, assign an actual number of seconds to each segment along the story arc "
                f"(opening and ending shorter, middle moderate, climax longest) and write it into that "
                f"segment's **Duration** field (whole seconds, never outside the range).")
    gen_lo, gen_hi = duration_lo + seam_runway, duration_hi + seam_runway
    lo_txt = f"{gen_lo:.2f}".rstrip("0").rstrip(".")
    hi_txt = f"{gen_hi:.2f}".rstrip("0").rstrip(".")
    if duration_lo == duration_hi:
        head = (f"Segment 1: {int(duration_lo)} s (normal opening, no head hand-off); "
                f"segment 2 onward: {lo_txt} s (= {int(duration_lo)} s final cut + {runway_txt} head hand-off)")
    else:
        segn = lo_txt if gen_lo == gen_hi else f"{lo_txt}–{hi_txt}"
        head = (f"Segment 1: {int(duration_lo)}–{int(duration_hi)} s (distributed along the arc, normal opening, "
                f"no head hand-off); segment 2 onward: {segn} s (= {int(duration_lo)}–{int(duration_hi)} s final cut "
                f"+ {runway_txt} head hand-off)")
    tail = (f" **Head hand-off section**: from segment 2 on, the first {runway_txt} of the segment must continue "
            f"the previous segment's last frame (same camera position / shot size / lighting / character pose and "
            f"direction of motion, action only slightly carried forward); this section is **cut away entirely** in "
            f"the final edit. So ① segment 2's [Shot 1] IS that continuation, and the first cut after it happens at "
            f"about {runway_txt}, where the segment's new content begins; ② the **Duration** field records the "
            f"ACTUAL GENERATED seconds (segment 1: {'{s1}'.format(s1=int(duration_lo)) if duration_lo == duration_hi else f'{int(duration_lo)}-{int(duration_hi)}'}, "
            f"segment 2 onward: {lo_txt if gen_lo == gen_hi else segn}, 1 decimal allowed); ③ the final cut length "
            f"= generated duration − {runway_txt} ≈ the duration you configured.")
    return head + tail


# ── 段首衔接相关（与中文版语义一致）──────────────────────────
def seam_edge_rule_en(seam_on, runway_txt):
    if seam_on:
        return (f"Segment boundary = hard cut + **head hand-off** (see the duration setting above): from segment 2 "
                f"on, [Shot 1] is the continuation of the previous segment's last frame, lasting {runway_txt} "
                f"(same camera position / shot size / lighting / character pose and direction of motion). This "
                f"section is cut away in the final edit, so the segment's new content starts after {runway_txt} "
                f"(the first cut lands at about {runway_txt}; you may change shot size / camera / space there). "
                f"Story and physical state stay continuous across segments (never regress, never replay an event "
                f"already shown); if not the last segment, state the physical end-state in 1–2 sentences as the "
                f"story hand-off.")
    return ("Segment boundary = hard cut / scene change: from segment 2 on, [Shot 1] is the next shot that directly "
            "follows the previous segment's story timeline (you may change shot size / camera / space) and must "
            "stand on its own; never write pseudo-references such as \"continuing the previous segment's last "
            "frame\". Story and physical state stay continuous across segments (never regress, never replay an "
            "event already shown); if not the last segment, state the physical end-state in 1–2 sentences as the "
            "story hand-off.")


def seam_check_rule_en(seam_on):
    if seam_on:
        return ("From segment 2 on, [Shot 1] is the continuation of the previous segment's last frame (duration = "
                "the head hand-off seconds) and the segment's new content follows it; no state regression, no "
                "replaying events already shown.")
    return ("From segment 2 on, [Shot 1] is the next shot directly following the previous segment's story timeline "
            "(hard cut / scene change, self-contained, no pseudo-references), and new content proceeds normally; "
            "no state regression, no replaying events already shown.")


def seam_line_en(seam_on, runway_txt):
    if seam_on:
        return (f"- Head hand-off (this film is generated segment by segment with physical stitching): from "
                f"segment 2 on, [Shot 1] MUST be the continuation of the previous segment's last frame — same "
                f"camera position / shot size / lighting / character body pose and direction of motion, action "
                f"only slightly carried forward — lasting {runway_txt}. This section is **cut away entirely** in "
                f"the final edit, so the segment's new content starts after {runway_txt} (the first cut lands at "
                f"about {runway_txt}; you may change shot size / camera / space). Segment 1 has no hand-off section "
                f"and opens normally. Story/physical state stays continuous: whoever was where, in what pose, and "
                f"what had just been discovered or triggered at the end of the previous segment must match the "
                f"hand-off exactly (never regress — if they already stood up / found the clue / stepped down, they "
                f"must not squat back / act unaware / step onto the same stair again), and never replay an event "
                f"already shown (each action happens once). If not the last segment, close with 1–2 sentences "
                f"stating the exact physical end-state as the story hand-off.")
    return ("- Hard-cut hand-off: each segment is generated independently — the boundary between segments is one "
            "hard cut / scene change: [Shot 1] of segment N+1 is the next shot right after the previous segment's "
            "story timeline (you may change shot size / camera / space); the segment must stand on its own and "
            "**must not** use pseudo-references such as \"continuing the previous segment's last frame\"; but "
            "story/character physical state MUST stay continuous: whoever was where, in what pose, and what had "
            "just been discovered or triggered at the end of the previous segment is the basis for the next "
            "segment (never regress — if they already stood up / found the clue / stepped down, they must not squat "
            "back / act unaware / step onto the same stair again), and never replay an event already shown. If not "
            "the last segment, close with 1–2 sentences stating the exact physical end-state as the story "
            "hand-off.")


def decompose_seam_variants_en(seam_on, runway_txt):
    """对应中文 decompose_rules 里 6 个 {Seam_*} 占位符的英文版。"""
    if seam_on:
        return {
            "Seam_Decompose_Bullet": (
                f"Head hand-off + hard cut: the whole film is generated segment by segment and then concatenated "
                f"(adjacent segments are physically stitched). From segment 2 on, the segment must open with a "
                f"continuation of the previous segment's last frame (same camera position / shot size / lighting / "
                f"character pose and direction of motion, lasting {runway_txt}); this section is cut away entirely in "
                f"the final edit, so the segment's new content starts after {runway_txt}. Character physical state must "
                f"stay continuous (the previous segment's end pose / position / discovered facts must carry over); "
                f"never regress state and never replay an event already shown; if not the last segment, state the "
                f"physical end-state clearly as the story hand-off."),
            "Seam_Principle_2": (
                "Head hand-off + story-state continuity: segments are continuous in the final cut (the next segment's "
                "opening is pinned to the previous segment's last frame). The key is that story/physical state must "
                "stay continuous: whoever was where, in what pose, injured or not, what had been discovered and done "
                "at the end of the previous segment must match the **head hand-off section** exactly (same camera "
                "position / shot size / lighting / pose), and the new content must continue from that state (you may "
                "change camera or present the same state at a different shot size). Never regress state (if they "
                "already stood up / found the clue / stepped down, they must not squat back / act unaware / step onto "
                "the same stair again) and never replay an event already shown (each action happens once). When "
                "cutting away to another space and later back to a scene, keep that scene's camera / shot size / "
                "lighting language consistent with its earlier shots."),
            "Seam_SelfContained": (
                f"**Head hand-off**: from segment 2 on, the first {runway_txt} is a continuation of the previous "
                f"segment's last frame (same camera position / shot size / lighting / character pose and direction of "
                f"motion, only slightly carried forward) — this section is cut away in the final edit, so write it "
                f"concretely (never the abstract phrase \"continuing the previous segment's last frame\"); the new "
                f"content of this segment comes after it. Segment 1 opens normally."),
            "Seam_Scheme_A": (
                f"Head hand-off (scheme A, cut away in the final edit): from segment 2 on, [Shot 1] MUST be written as "
                f"\"the continuation of the previous segment's last frame\" — same camera position / shot size / "
                f"lighting / character body pose and direction of motion, action only slightly carried forward, "
                f"lasting {runway_txt}, and the previous end-state must be written out as a concrete picture (even if "
                f"barely moving); this section is cut away entirely, and only after it does the segment cut into its new "
                f"content (the first cut lands at about {runway_txt})."),
            "Seam_No_Pseudo_Ref": (
                "No abstract pseudo-references: never write \"continuing the previous segment's last frame / picking up "
                "the previous ending / the previous segment continues\" — the hand-off section must describe concrete "
                "imagery (who / where / what pose / how far the action has progressed / lighting and environment), "
                "because the next segment's opening is really pinned to the previous last frame."),
            "Seam_Ban_Pseudo_Ref": (
                "No abstract cross-segment imagery (never write \"continuing the previous segment's last frame / the "
                "previous segment continues / picking up where we left off\"): the head hand-off must describe "
                "concrete imagery (who / where / what pose / how far the action has progressed / lighting and "
                "environment), because the next segment's opening is really pinned to the previous last frame; new "
                "content must stand on its own."),
        }
    return {
        "Seam_Decompose_Bullet": (
            "Head hand-off + hard cut: the whole film is generated segment by segment and then concatenated (the "
            "boundary between segments is one hard cut / scene change). From segment 2 on, [Shot 1] directly writes "
            "the next shot that follows the previous segment's story timeline (you may change shot size / camera / "
            "space); the segment must stand on its own and must never use pseudo-references such as \"continuing the "
            "previous segment's last frame\". Character physical state must stay continuous (the previous segment's "
            "end pose / position / discovered facts must carry over); never regress state and never replay an event "
            "already shown; if not the last segment, state the physical end-state clearly as the story hand-off."),
        "Seam_Principle_2": (
            "Segment hand-off + story-state continuity: segment boundaries are hard cuts / scene changes. The key is "
            "that story/physical state must stay continuous: whoever was where, in what pose, injured or not, what "
            "had been discovered and done at the end of the previous segment is the basis of this segment's opening "
            "(you may change camera or present the same state at a different shot size). Never regress state (if they "
            "already stood up / found the clue / stepped down, they must not squat back / act unaware / step onto the "
            "same stair again) and never replay an event already shown (each action happens once). When cutting away "
            "to another space and later back to a scene, keep that scene's camera / shot size / lighting language "
            "consistent with its earlier shots."),
        "Seam_SelfContained": (
            "**Segment hand-off**: boundaries are handled as hard cuts / scene changes; from segment 2 on, write the "
            "next shot that directly follows the previous segment's story timeline (you may change shot size / camera "
            "/ space); the segment must stand on its own and must never use pseudo-references such as \"continuing "
            "the previous segment's last frame\". Segment 1 opens normally."),
        "Seam_Scheme_A": (
            "Hard cut between segments: from segment 2 on, [Shot 1] directly writes the next shot that follows the "
            "previous segment's story timeline (you may change shot size / camera / space); the segment stands on its "
            "own, and pseudo-references such as \"continuing the previous segment's last frame\" are banned."),
        "Seam_No_Pseudo_Ref": (
            "No abstract pseudo-references: never write \"continuing the previous segment's last frame / picking up "
            "the previous ending / the previous segment continues\" — every segment's opening describes concrete "
            "imagery directly (who / where / what pose / how far the action has progressed / lighting and environment)."),
        "Seam_Ban_Pseudo_Ref": (
            "No abstract cross-segment imagery (never write \"continuing the previous segment's last frame / the "
            "previous segment continues / picking up where we left off\"): every segment's opening must describe "
            "concrete imagery (who / where / what pose / how far the action has progressed / lighting and "
            "environment); new content stands on its own."),
    }


def timing_plan_en(duration_lo, duration_hi, duration_desc, seam_on, runway_txt, seg_count_ref):
    """对应中文 timing_plan 的英文版。"""
    if seam_on:
        dur_line = f"- Duration setting: {duration_desc}"
    elif duration_lo == duration_hi:
        dur_line = f"- Duration setting: global fixed, every segment's **Duration** is exactly {int(duration_lo)} s."
    else:
        dur_line = (f"- Duration setting: each segment lasts {int(duration_lo)}–{int(duration_hi)} s. Before "
                    f"decomposing, assign an actual number of seconds to each segment along the story arc (opening "
                    f"and ending shorter, middle moderate, climax longest) and write it into that segment's "
                    f"**Duration** field (whole seconds, never outside the range).")
    return (
        "Direct the whole short drama as one continuous film:\n"
        + dur_line + "\n"
        + "- Cut budget: [Shot N] count per segment = segment duration ÷ per-shot duration (a shot is usually "
          "≥ 0.8–1 s; normal speech is about 4–5 Chinese chars or 2–3 English words per second). First estimate the "
          f"total cut budget of the film ({seg_count_ref} segments × duration ÷ per-shot duration), then distribute "
          "cut density along the arc: fewer, longer shots in the opening (establishing), progression in the middle "
          "(2–4 shots per segment), rapid cuts at the climax, settling at the end. Dialogue-heavy segments get fewer, "
          "longer shots (drama first: every line plus the speaker's expression and the listener's reaction must be "
          "complete, then add action / camera / environment). Never cut the same action twice and never cut just to "
          "hit a count.\n"
        + seam_line_en(seam_on, runway_txt)
    )


# ── 素材标签对照表（英文）────────────────────────────────────
_MATERIAL_TYPES_EN = {
    "角色": "Character", "场景": "Scene", "道具": "Prop", "视频": "Video", "音频": "Audio",
    "音色": "Voice", "配乐": "Score", "念白": "Narration", "分镜": "Storyboard",
    "音效": "SFX", "音乐": "Music", "其他": "Other",
}


def material_table_en(ref_image_intro, ref_video_intro, ref_audio_intro, re_mod):
    """对应中文 `_build_material_table` 的英文版。"""
    blocks = []
    for kind, intro in (("Images", ref_image_intro), ("Videos", ref_video_intro), ("Audio", ref_audio_intro)):
        if not intro or not intro.strip():
            continue
        rows = []
        for line in str(intro).splitlines():
            line = line.strip()
            if not line:
                continue
            m = re_mod.match(r'^(角色|场景|道具|视频|音频|音色|配乐|念白|分镜|音效|音乐|其他)\s*([A-Za-z])\s*[=＝:：]\s*(.+)', line)
            if not m:
                continue
            typ, slot, rest = m.group(1), m.group(2).upper(), m.group(3).strip()
            if not rest:
                continue
            rows.append(f"- {_MATERIAL_TYPES_EN.get(typ, typ)}{slot} = {rest}")
        if rows:
            blocks.append(f"【{kind}】\n" + "\n".join(rows))
    if not blocks:
        return ""
    return ("## 3.5 Reference-material label table (CRITICAL — the ONLY source of slots; slots may only take "
            "\"type:slotName\" from here)\n"
            + "\n".join(blocks)
            + "\n\n## 3.6 Material whitelist (CRITICAL — header fields and scheduling must never reference anything "
              "outside the table above)\n"
            "- Every name listed in a segment's header metadata (**Characters**/**Scenes**/**Props**/**Videos**/"
              "**Audio**/**SFX**/**Music**/**Other**) must come from the \"material name\" or \"type:slotName\" "
              "columns above (e.g. 场景:场景B, 角色:角色A). Never invent or add any.\n"
            "- If the plot needs a material that is not in the table (a prop / scene / character), do NOT put it in "
              "header fields or scheduling slots: write \"none\" there and only describe it as environment inside "
              "the detailed_description body.\n"
            "- Scheduling slots are written as \"type:slotName\", e.g. 场景:场景A, 角色:角色A, 道具:道具A, 视频:视频A, "
              "音频:音色A (the type prefix for all audio sub-kinds is 音频; the slot name may be 音色A / 配乐A / 念白A / "
              "音频A). Never write a material name (e.g. 孙悟空) and never invent a slot.\n"
            "- subject_definitions uses the material name (<Subject N> is 孙悟空 in <Picture N>); the appearance "
              "description in parentheses is restated verbatim.\n"
            "- Audio slots map to the speaker's voice: whoever speaks in this segment gets the matching slot in "
              "AUDIO_INSTRUCTION.slots (character voice → \"音频:音色X\", other audio → \"音频:配乐X / 念白X / 音频X\" — "
              "always copy the slot name from the table). Segments without dialogue leave audio slots empty.")
