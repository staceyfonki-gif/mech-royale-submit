"""Survival-focused bot for mechfight."""
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
        ("hp", 5.0),
        ("health", 5.0),
        ("armor", 4.5),
        ("speed", 4.0),
        ("move", 3.5),
        ("boost", 3.5),
        ("energy", 3.5),
        ("cooldown", 3.0),
        ("reload", 2.5),
        ("damage", 2.5),
        ("range", 2.0),
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
    name = "kestrel-v2"

    def __init__(self, hello: mf.Hello):
        cfg = hello.get("config", {})
        self.reach = cfg.get("loot", {}).get("pickup_radius", 4.0)

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = me["pos"]
        hp_ratio = me["hp"] / max(1.0, me.get("max_hp", 100.0))
        energy = me.get("energy", 0.0)
        in_zone = bool(me.get("in_zone", True))

        zone = obs["zone"]
        next_cx, next_cy = zone["next_center"]
        next_r = max(1.0, float(zone.get("next_radius", 50.0)))
        dist_next = math.hypot(next_cx - x, next_cy - y)
        t = float(obs.get("t", 0.0))
        shrinking = float(zone.get("shrink_start", 0.0)) <= t <= float(zone.get("shrink_end", 0.0))

        act: mf.Action = {"tick": obs["tick"]}

        # 1. Pick strongest survival/mobility upgrade if offered
        offer = obs.get("offer")
        if offer and offer.get("choices"):
            best_idx = max(
                range(len(offer["choices"])),
                key=lambda i: _score_choice(offer["choices"][i]),
            )
            act["pick"] = best_idx

        # 2. Grab a weapon into empty slot 1 when in pickup range
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

        # 3. Movement: get inside next zone early/cleanly, kite low-HP close fights
        to_zone_yaw = math.atan2(next_cy - y, next_cx - x)
        must_push_zone = (not in_zone) or (shrinking and dist_next > next_r * 0.65) or (dist_next > next_r * 0.85)

        enemies = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]
        enemies.sort(key=lambda m: math.dist(m["pos"], me["pos"]))

        if must_push_zone:
            move_yaw = to_zone_yaw
            want_boost = (not in_zone) and energy > 24.0
        elif enemies and hp_ratio < 0.48 and math.dist(enemies[0]["pos"][:2], (x, y)) < 24.0:
            away_yaw = math.atan2(y - enemies[0]["pos"][1], x - enemies[0]["pos"][0])
            cand_x = x + math.cos(away_yaw) * 10.0
            cand_y = y + math.sin(away_yaw) * 10.0
            if math.hypot(next_cx - cand_x, next_cy - cand_y) < next_r * 0.88:
                move_yaw = away_yaw
            else:
                move_yaw = to_zone_yaw
            want_boost = energy > 35.0
        elif dist_next > next_r * 0.35:
            move_yaw = to_zone_yaw
            want_boost = False
        else:
            strafe_dir = 1.0 if int(obs["tick"]) % 80 < 40 else -1.0
            move_yaw = to_zone_yaw + strafe_dir * (math.pi * 0.45)
            want_boost = False

        act["move"] = [math.cos(move_yaw), math.sin(move_yaw)]
        act["boost"] = want_boost
        act["jump"] = int(obs["tick"]) % 35 == 0 and energy > 18.0

        # 4. Aiming & firing only when in range, aligned, and ready to shoot
        cur_yaw = me.get("aim", [0.0, 0.0])[0]
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
            if horiz_dist < 82.0 and yaw_err < 0.30:
                f0 = _can_fire_slot(weapons, 0)
                f1 = _can_fire_slot(weapons, 1)
                if f0 or f1:
                    act["fire"] = [f0, f1]
                    mf.draw.line([x, y, z + 6.0], [px, py, tz + 3.5], "#ff6040")

        mf.draw.label(f"hp {me['hp']:.0f} | zone {'IN' if in_zone else 'OUT'}")
        return act


mf.run(Bot)
