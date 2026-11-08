# -*- coding: utf-8 -*-
"""镜头语言偏好 —— 英文版提示词（键仍用中文选项值，值与工作流保存值一一对应）。"""

SHOT_SIZE_HINTS_EN = {
    "根据剧情": "Choose shot sizes for the story: give every [Shot N] the most fitting size and progress through extreme long / long / medium / close-up / extreme close-up as the plot advances (establish → develop → climax → settle); never use only one or two for the whole segment",
    "随机组合": "Mix several shot sizes inside one video (extreme long / long / medium / close-up / extreme close-up changing with the plot). Different [Shot N] use different sizes and never one size for the whole segment; scale with duration and story — long videos (10 s+) progress through 4-6 sizes (establish → develop → climax → settle), short videos need only 2-3",
    "远景为主": "Mostly extreme long / long shots: the character occupies a small part of the frame, emphasising environment, spatial relations and overall atmosphere",
    "全景为主": "Mostly full shots: show the character's whole body and the whole scene, establishing the relation between people and space",
    "中景为主": "Mostly medium shots: frame from the waist up, balancing body action and facial expression",
    "近景为主": "Mostly close shots: frame from the chest up, emphasising expression, emotion and detail",
    "特写为主": "Mostly close-ups: focus on the face, hands or a key prop to stress the decisive moment and impact",
    "大特写为主": "Mostly extreme close-ups (macro): frame eyes, lips, fingertips or part of a key prop at the closest distance for maximum impact and psychological pressure",
}

CAMERA_HINTS_EN = {
    "根据剧情": "Choose camera moves for the story: give every [Shot N] the most fitting move and vary it with the plot (slow pan for establishing, orbit for standoff, fast push/pull for bursts, tracking for pursuit); never use only one or two for the whole segment",
    "随机组合": "Mix several camera moves inside one video (push / pull / pan / truck / tracking / orbit / handheld interleaved). Different [Shot N] use different moves and never one move for the whole segment; scale with duration and story — long videos use more moves (4-6) following the dramatic curve, short videos need only 2-3",
    "固定机位": "Mostly static camera: tell the story through subject action and composition change, without relying on camera movement",
    "推拉": "Mostly push-in / pull-out: push-in emphasises, pull-out reveals the environment",
    "摇移": "Mostly pan / truck: reveal space and the relation between subjects horizontally",
    "俯仰": "Mostly tilt (up/down): reveal height, scale and vertical relations",
    "升降": "Mostly crane / pedestal: rise or descend to reveal scale and change the vantage point",
    "环绕": "Mostly orbit / arc: circle the subject to build tension or reveal it in the round",
    "跟拍": "Mostly tracking: follow the subject's movement to keep motion energy and immersion",
    "手持晃动": "Mostly handheld with slight shake: documentary realism and nervous energy",
    "旋转": "Mostly roll / rotation: tilt or spin the frame for disorientation and urgency",
    "一镜到底": "Mostly one continuous take: no cuts, the camera alone changes the viewpoint",
}

ANGLE_HINTS_EN = {
    "根据剧情": "Choose the camera angle for the story: pick the most fitting angle per [Shot N] and progress it with the plot",
    "随机组合": "Mix angles inside one video (eye level / high / low / over-the-shoulder / POV …) so different [Shot N] use different angles",
    "视线平视": "Mostly eye level: the camera sits at the subject's eye height for a neutral, immersive, equal-footing feel",
    "高角度俯拍": "Mostly high angle looking down: makes the subject look small, pressured or observed",
    "鸟瞰顶视": "Mostly bird's-eye / top-down: a god's-eye layout that shows spatial relations and isolation",
    "低角度仰拍": "Mostly low angle looking up: makes the subject dominant, heroic or threatening",
    "虫眼极低": "Mostly worm's-eye from near the ground: extreme upward distortion for overwhelming scale or surreal menace",
    "航拍视角": "Mostly aerial / drone: sweeping views for scale, geography and spectacle",
    "第一人称POV": "Mostly first-person POV: see through the character's eyes for maximum identification",
    "过肩镜头": "Mostly over-the-shoulder: frames the listener past the speaker's shoulder, standard for dialogue",
    "第三人称跟随": "Mostly third-person follow: the camera trails the character as an observer",
    "偷窥视角": "Mostly voyeuristic view through a gap or blinds, creating suspense and the feeling of being watched",
    "监控摄像头": "Mostly CCTV / security-camera angle: high wide fixed shot with slight distortion for documentary realism and surveillance",
    "手机自拍": "Mostly selfie / front-camera handheld: close wide angle with slight shake for a real social-media feel",
    "正反打": "Mostly reverse angle, shot-reverse-shot: alternating opposite angles for dialogue",
    "侧面视角": "Mostly profile / side view: camera roughly 90° to the character's gaze, stressing silhouette and trajectory",
    "背身视角": "Mostly rear view: the camera faces the character's back, stressing direction, the unknown ahead and isolation",
    "四分之三侧": "Mostly three-quarter view: keeps both facial information and spatial depth — the most natural all-purpose angle",
    "镜像反射": "Mostly view seen in a mirror reflection: through mirrors, water or glass for fragmentation, introspection or duality",
    "斜角荷兰角": "Mostly Dutch angle: tilt the horizon (express with Roll Clockwise / Roll Counterclockwise); only for pursuit, failure, shock or absurdity",
    "希区柯克变焦": "Mostly dolly zoom (camera pushes in while zooming out): subject size stays constant while the background rushes; use with a static camera for stability",
    "鱼眼广角": "Mostly fisheye wide angle: strong edge distortion for exaggeration, comedy or dizziness",
    "框中框构图": "Mostly frame within a frame: use doorways, windows, gaps, mirrors or foreground occlusion to enclose the subject",
    "长焦压缩视角": "Mostly telephoto compression: compress foreground/background distance and press the background onto the subject — good for standoffs, stares and crowds",
}

CUT_HINTS_EN = {
    "根据剧情": "Cut count follows the story: fights / chases / bursts cut fast by default (2-4 s per shot), lyrical or static passages slow down; never distribute cuts evenly — long and short shots alternate with the dramatic curve",
    "随机": "Choose the cut count and rhythm freely from the story: fights / chases / bursts cut fast by default (2-4 s per shot), lyrical or static passages slow down; never distribute cuts evenly — long and short shots alternate with the dramatic curve",
    "随机组合": "Mixed cut rhythm inside one video: fast cuts during fights (2-4 s per shot) interleaved with long takes; extremely still while winding up, extremely fast at the burst; never evenly distributed. Adjust with the story — tense passages cut fast, calm passages cut slow, long and short shots alternate, never one rhythm throughout",
    "一镜到底": "One continuous take: write only one [Shot 1] for this segment, no cuts, change the viewpoint with camera movement alone",
    "2~5镜": "Split this segment into 2-5 [Shot N] cuts (timestamps inside the segment)",
    "5~9镜": "Split this segment into 5-9 [Shot N] cuts (timestamps inside the segment)",
    "9~13镜": "Split this segment into 9-13 [Shot N] cuts (timestamps inside the segment)",
    "13~18镜": "Split this segment into 13-18 [Shot N] cuts (timestamps inside the segment)",
}

TRANSITION_HINTS_EN = {
    "硬切": "Use hard cuts only, no transition effects; every cut must introduce new information (subject / space / state / angle / time)",
    "叠化": "Use cross-dissolves: good for time passing or emotional bridging",
    "淡入淡出": "Use fades: good for openings, endings or section changes",
    "擦除": "Use wipes: good for space changes or brisk passages",
}

CREATIVE_HINTS_EN = {
    "无特别要求": "",
    "节奏紧凑": "Tight pacing, high visual information density, every shot advances the plot, no dragging",
    "舒缓留白": "Relaxed pacing, plenty of negative space and breathing room, emotion settles in silence and detail",
    "情感细腻": "Delicate emotion, focusing on inner drama, micro-expressions and emotional negative space",
    "明快轻松": "Bright, light-hearted tone with brisk, lively rhythm and a cheerful, unoppressive mood",
    "多反转结局": "Multiple twists, escalating layer by layer, constantly overturning audience expectations",
    "开放式结局": "Open ending that leaves room for the audience's imagination",
    "强冲突": "Sharp, direct character conflict with strong tension throughout",
}

DETAIL_LENGTH_HINTS_EN = {
    "标准 (350-500字)": "350-500",
    "精简 (200-350字)": "200-350",
    "详细 (500-800字)": "500-800",
    "超详细 (800-1200字)": "800-1200",
}
