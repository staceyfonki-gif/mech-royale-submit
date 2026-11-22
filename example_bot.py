"""Squad-aware survival bot for mechfight."""
import math
import mechfight as mf


def _wrap_angle(a: float) -> float:
    while a > math.pi:
        a -= 2.0 * math.pi
    while a < -math.pi:
        a += 2.0 * math.pi
    return a


def _score_choice(choice: dict) -> float:
    text = f"{choice.get('id', '')} {choice.get('name', '')} {choice.get('desc', '')}".lower()
    score = 0.0
    for kw, pts in (
        ("hp", 6.0),
        ("health", 6.0),
        ("shield", 5.5),
        ("armor", 5.0),
        ("regen", 5.0),
        ("repair", 5.0),
        ("speed", 4.5),
        ("move", 4.0),
        ("boost", 4.0),
        ("energy", 4.0),
        ("cooldown", 3.0),
        ("reload", 2.5),
        ("damage", 2.0),
        ("range", 1.5),
    ):
        if kw in text:
            score += pts
    return score


def _can_fire_slot(weapons: list, idx: int) -> bool:
    if idx >= len(weapons) or weapons[idx] is None:
        return False
    w = weapons[idx]
    if float(w.get("reload", 0.0)) > 0.0:
        return False
    ammo = w.get("ammo")
    if ammo is not None and float(ammo) <= 0.0:
        return False
    return True


class Bot:
    name = "kestrel-v3"

    def __init__(self, hello: mf.Hello):
        cfg = hello.get("config", {})
        self.reach = cfg.get("loot", {}).get("pickup_radius", 4.0)
        self.strafe_dir = 1.0 if int(hello.get("seed", 0)) % 2 == 0 else -1.0

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = me["pos"]
        hp = float(me.get("hp", 100.0))
        max_hp = max(1.0, float(me.get("max_hp", 100.0)))
        hp_ratio = hp / max_hp
        energy = float(me.get("energy", 0.0))
        in_zone = bool(me.get("in_zone", True))

        zone = obs["zone"]
        next_cx, next_cy = zone["next_center"]
        next_r = max(1.0, float(zone.get("next_radius", 50.0)))
        dist_next = math.hypot(next_cx - x, next_cy - y)
        t = float(obs.get("t", 0.0))
        shrink_start = float(zone.get("shrink_start", 0.0))
        shrink_end = float(zone.get("shrink_end", 0.0))
        zone_dps = float(zone.get("dps", 0.0))
        shrinking = shrink_start <= t <= shrink_end

        events = obs.get("events", [])
        took_hit = any(e.get("type") == "damaged" for e in events)
        hit_bearings = [
            float(e["bearing"])
            for e in events
            if e.get("type") == "damaged" and e.get("bearing") is not None
        ]

        act: mf.Action = {"tick": obs["tick"]}

        # 1. Pick strongest survival / mobility / energy upgrade
        offer = obs.get("offer")
        if offer and offer.get("choices"):
            best_idx = max(
                range(len(offer["choices"])),
                key=lambda i: _score_choice(offer["choices"][i]),
            )
            act["pick"] = best_idx

        # 2. Grab a weapon into empty slot 1 when within pickup radius
        weapons = me.get("weapons", [])
        if len(weapons) > 1 and weapons[1] is None:
            near = [
                p for p in obs.get("pickups", [])
                if math.dist(p["pos"][:2], (x, y)) <= self.reach
            ]
            if near:
                non_gun = [p for p in near if p.get("kind") != "gun"]
                chosen = non_gun[0] if non_gun else near[0]
                act["take"] = chosen["id"]

        # Flip lateral strafe direction periodically or when hit
        tick_int = int(obs["tick"])
        if took_hit or tick_int % 65 == 0:
            self.strafe_dir *= -1.0

        enemies = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]
        enemies.sort(key=lambda m: math.dist(m["pos"], me["pos"]))
        nearest_dist = math.hypot(enemies[0]["pos"][0] - x, enemies[0]["pos"][1] - y) if enemies else 999.0

        # 3. Movement: survival & zone safety first (avoid becoming an easy squad target)
        to_zone_yaw = math.atan2(next_cy - y, next_cx - x)
        urgent_zone = (not in_zone) or (shrinking and dist_next > next_r * 0.62) or (dist_next > next_r * 0.82)

        def _inside_safe_next(yaw: float, step: float = 10.0) -> bool:
            nx = x + math.cos(yaw) * step
            ny = y + math.sin(yaw) * step
            return math.hypot(next_cx - nx, next_cy - ny) < next_r * 0.86

        if urgent_zone:
            move_yaw = to_zone_yaw
            want_boost = (not in_zone or zone_dps >= 4.0) and energy > 22.0
        elif hp_ratio < 0.55 and enemies and nearest_dist < 32.0:
            away_yaw = math.atan2(y - enemies[0]["pos"][1], x - enemies[0]["pos"][0])
            evade_yaw = away_yaw + self.strafe_dir * 0.45
            if _inside_safe_next(evade_yaw):
                move_yaw = evade_yaw
            elif _inside_safe_next(away_yaw):
                move_yaw = away_yaw
            else:
                move_yaw = to_zone_yaw
            want_boost = energy > 28.0
        elif took_hit and hit_bearings:
            perp_yaw = hit_bearings[0] + self.strafe_dir * (math.pi * 0.5)
            move_yaw = perp_yaw if _inside_safe_next(perp_yaw) else to_zone_yaw
            want_boost = hp_ratio < 0.65 and energy > 30.0
        elif dist_next > next_r * 0.35:
            move_yaw = to_zone_yaw
            want_boost = False
        else:
            strafe_yaw = to_zone_yaw + self.strafe_dir * (math.pi * 0.48)
            move_yaw = strafe_yaw if _inside_safe_next(strafe_yaw, 8.0) else to_zone_yaw
            want_boost = False

        act["move"] = [math.cos(move_yaw), math.sin(move_yaw)]
        act["boost"] = want_boost
        act["jump"] = (tick_int % 30 == 0 and energy > 18.0) or (took_hit and energy > 24.0)

        # 4. Aiming & selective firing (save energy/attention when hurting)
        cur_yaw = float(me.get("aim", [0.0, 0.0])[0])
        act["aim"] = [to_zone_yaw, 0.0]

        if enemies:
            t_mech = enemies[0]
            tx, ty, tz = t_mech["pos"]
            tvx, tvy, _ = t_mech.get("vel", [0.0, 0.0, 0.0])
            horiz_dist = math.hypot(tx - x, ty - y)
            lead_t = min(0.6, horiz_dist / 90.0)
            px, py = tx + tvx * lead_t, ty + tvy * lead_t
            aim_yaw = math.atan2(py - y, px - x)
            aim_pitch = math.atan2(tz - z - 2.0, max(1.0, math.hypot(px - x, py - y)))
            act["aim"] = [aim_yaw, aim_pitch]

            yaw_err = abs(_wrap_angle(aim_yaw - cur_yaw))
            safe_to_engage = (hp_ratio >= 0.32) or (horiz_dist < 16.0)
            if safe_to_engage and horiz_dist < 72.0 and yaw_err < 0.28:
                f0 = _can_fire_slot(weapons, 0)
                f1 = _can_fire_slot(weapons, 1)
                if f0 or f1:
                    act["fire"] = [f0, f1]
                    mf.draw.line([x, y, z + 6.0], [px, py, tz + 3.5], "#ff6040")

        mf.draw.label(f"hp {hp:.0f} | zone {'IN' if in_zone else 'OUT'}")
        return act


mf.run(Bot)
