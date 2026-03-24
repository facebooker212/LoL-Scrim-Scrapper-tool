import pandas as pd
import json
import os
import ast
from collections import defaultdict

TEAMFIGHT_WINDOW = 15
TEAMFIGHT_MIN_KILLS = 3

POSITION_ORDER = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]

TEAM_COLORS = {
    "ORDER": "#4A90D9",
    "CHAOS": "#E05C5C",
}


def normalize_name(name):
    if not isinstance(name, str):
        return name
    return name.split("#")[0].strip()


def load_match(folder):
    metadata_path = os.path.join(folder, "metadata.json")
    events_path = os.path.join(folder, "events.csv")
    timeline_path = os.path.join(folder, "player_timeline.csv")

    with open(metadata_path) as f:
        metadata = json.load(f)

    events = pd.read_csv(events_path)
    timeline = pd.read_csv(timeline_path)

    players = pd.DataFrame(metadata["players"])
    players["normalized"] = players["summonerName"].apply(normalize_name)

    # Parse items column from JSON string back to list of dicts
    timeline["items"] = timeline["items"].apply(
        lambda x: json.loads(x) if isinstance(x, str) and x.strip() else []
    )

    return metadata, players, events, timeline


def preprocess(players, events, timeline):
    timeline = timeline.copy()
    events = events.copy()

    timeline["minute"] = timeline["timestamp"] / 60
    events["minute"] = events["EventTime"] / 60

    # Drop columns from timeline that will come from players to avoid duplicates
    for col in ["position", "keystone", "primary_rune_tree", "secondary_rune_tree",
                "summoner_spell_1", "summoner_spell_2"]:
        if col in timeline.columns:
            timeline = timeline.drop(columns=[col])

    timeline = timeline.merge(
        players[["summonerName", "normalized", "position", "keystone",
                 "primary_rune_tree", "secondary_rune_tree",
                 "summoner_spell_1", "summoner_spell_2"]],
        left_on="player",
        right_on="summonerName",
        how="left",
    )

    kills = events[events["EventName"] == "ChampionKill"].copy()
    turrets = events[events["EventName"] == "TurretKilled"].copy()
    inhibs = events[events["EventName"] == "InhibKilled"].copy()

    return timeline, kills, turrets, inhibs


def parse_assisters(raw):
    if not isinstance(raw, str) or raw.strip() == "":
        return []
    try:
        parsed = ast.literal_eval(raw)
        if isinstance(parsed, list):
            return parsed
    except:
        pass
    return []


def detect_teamfights(kills):
    fights = []
    current = []

    for _, row in kills.sort_values("EventTime").iterrows():
        if not current:
            current.append(row)
            continue

        if row["EventTime"] - current[-1]["EventTime"] <= TEAMFIGHT_WINDOW:
            current.append(row)
        else:
            if len(current) >= TEAMFIGHT_MIN_KILLS:
                fights.append(current)
            current = [row]

    if len(current) >= TEAMFIGHT_MIN_KILLS:
        fights.append(current)

    return fights


def reconstruct_fights(fights, players):
    player_team = dict(zip(players["normalized"], players["team"]))
    results = []

    for fight in fights:
        order_kills = 0
        chaos_kills = 0
        participants = set()

        start = fight[0]["EventTime"]
        end = fight[-1]["EventTime"]

        for e in fight:
            killer = normalize_name(e["KillerName"])
            victim = normalize_name(e["VictimName"])

            participants.add(killer)
            participants.add(victim)

            for a in parse_assisters(e.get("Assisters")):
                participants.add(normalize_name(a))

            if player_team.get(killer) == "ORDER":
                order_kills += 1
            elif player_team.get(killer) == "CHAOS":
                chaos_kills += 1

        winner = "ORDER" if order_kills > chaos_kills else "CHAOS"

        results.append({
            "start": start,
            "end": end,
            "duration": end - start,
            "kills": len(fight),
            "winner": winner,
            "participants": list(participants),
        })

    return results


def compute_fight_breakdown(fights, players):
    player_team = dict(zip(players["normalized"], players["team"]))
    fight_details = []

    for fight in fights:
        stats = defaultdict(lambda: {"k": 0, "d": 0, "a": 0})

        for e in fight:
            killer = normalize_name(e["KillerName"])
            victim = normalize_name(e["VictimName"])

            stats[killer]["k"] += 1
            stats[victim]["d"] += 1

            for a in parse_assisters(e.get("Assisters")):
                stats[normalize_name(a)]["a"] += 1

        order_players = []
        chaos_players = []

        for player, s in stats.items():
            team = player_team.get(player)
            entry = {"player": player, "k": s["k"], "d": s["d"], "a": s["a"]}
            if team == "ORDER":
                order_players.append(entry)
            elif team == "CHAOS":
                chaos_players.append(entry)

        fight_details.append({
            "start": fight[0]["EventTime"],
            "end": fight[-1]["EventTime"],
            "order": order_players,
            "chaos": chaos_players,
        })

    return fight_details


def kill_participation(kills, players):
    team_map = dict(zip(players["normalized"], players["team"]))
    team_kills = defaultdict(int)
    kp = defaultdict(int)

    for _, row in kills.iterrows():
        killer = normalize_name(row["KillerName"])
        if killer not in team_map:
            continue
        team = team_map[killer]
        team_kills[team] += 1
        kp[killer] += 1

        for a in parse_assisters(row.get("Assisters")):
            a = normalize_name(a)
            if a in team_map:
                kp[a] += 1

    results = {}
    for _, player in players.iterrows():
        name = player["normalized"]
        team = player["team"]
        total = team_kills[team]
        results[player["summonerName"]] = kp[name] / total if total > 0 else 0

    return results


def cs_per_minute(timeline):
    last_frame = timeline.sort_values("timestamp").groupby("player").tail(1)
    cs_min = {}

    for _, r in last_frame.iterrows():
        minutes = r["timestamp"] / 60
        cs_min[r["player"]] = r["cs"] / minutes if minutes > 0 else 0

    return cs_min


def final_stats(timeline, players):
    last = timeline.sort_values("timestamp").groupby("player").tail(1).copy()
    # Drop columns that exist in both dataframes to avoid duplicate column conflicts
    for col in ["position", "champion", "team"]:
        if col in last.columns:
            last = last.drop(columns=[col])
    last = last.merge(
        players[["summonerName", "team", "champion", "position"]],
        left_on="player",
        right_on="summonerName",
        how="left",
    )
    return last


# Actual turret name format observed from the Live Client Data API:
# Turret_TChaos_L0_P3_<uid>_0  or  Turret_TOrder_L1_P2_<uid>_0
# TChaos / TOrder  = owning team
# L0 / L1 / L2    = lane (top / mid / bot)
# P1 / P2 / P3    = tier (inhib turret / inner / outer); P4/P5 are base turrets
TURRET_TEAM_MAP = {"TChaos": "CHAOS", "TOrder": "ORDER"}
TURRET_LANE_MAP = {"L0": "Top", "L1": "Mid", "L2": "Bot"}
TURRET_TIER_MAP = {"P1": "Inhibitor", "P2": "Inner", "P3": "Outer"}


def parse_turret_name(raw_name):
    """
    Parse the TurretKilled name string into owning team, destroying team, lane and tier.
    Format: Turret_TChaos_L0_P3_<uid>_0
    The destroying team is always the OPPOSITE of the turret's owning team.
    Returns a dict with keys: owned_by, destroyed_by, lane, tier, label.
    Returns None if the name can't be parsed.
    """
    if not isinstance(raw_name, str):
        return None

    parts = raw_name.strip().split("_")
    # Minimum expected: Turret, TOrder/TChaos, L0/L1/L2, P1/P2/P3
    if len(parts) < 4:
        return None

    team_code = parts[1]  # TChaos or TOrder
    lane_code = parts[2]  # L0, L1, L2
    tier_code = parts[3]  # P1, P2, P3, P4, P5

    owned_by = TURRET_TEAM_MAP.get(team_code)
    if not owned_by:
        return None

    destroyed_by = "CHAOS" if owned_by == "ORDER" else "ORDER"
    lane = TURRET_LANE_MAP.get(lane_code, lane_code)
    tier = TURRET_TIER_MAP.get(tier_code, f"Base ({tier_code})")

    return {
        "owned_by": owned_by,
        "destroyed_by": destroyed_by,
        "lane": lane,
        "tier": tier,
        "label": f"{lane} {tier}",
    }


def _killer_to_team(killer_name, player_team_map):
    """
    Resolve a KillerName to ORDER or CHAOS.
    - Player names are looked up in the player-team map.
    - Minion_T100* = ORDER minion, Minion_T200* = CHAOS minion.
    - Falls back to None if unresolvable.
    """
    if not isinstance(killer_name, str):
        return None

    normalized = normalize_name(killer_name)

    # Player lookup
    if normalized in player_team_map:
        return player_team_map[normalized]

    # Minion naming: T100 = ORDER (blue), T200 = CHAOS (red)
    upper = killer_name.upper()
    if "MINION_T100" in upper:
        return "ORDER"
    if "MINION_T200" in upper:
        return "CHAOS"

    return None


def enrich_turrets(turrets_raw, players):
    """
    Add destroyed_by, owned_by, lane and tier columns to turret events.

    Priority for destroyed_by:
      1. KillerName resolved via player-team map (most reliable)
      2. KillerName resolved via minion team code (T100/T200)
      3. Turret name parsing — destroying team = opposite of owning team (fallback)
    """
    turrets = turrets_raw.copy()
    player_team_map = dict(zip(players["normalized"], players["team"]))

    rows = []
    for _, row in turrets.iterrows():
        parsed = parse_turret_name(row.get("TurretKilled"))

        # Try to resolve destroying team from KillerName first
        killer_team = _killer_to_team(row.get("KillerName"), player_team_map)

        if killer_team:
            destroyed_by = killer_team
        elif parsed:
            # Fallback: whoever owns the turret, the other team destroyed it
            destroyed_by = parsed["destroyed_by"]
        else:
            destroyed_by = "Unknown"

        rows.append({
            "EventTime":    row["EventTime"],
            "TurretKilled": row.get("TurretKilled", ""),
            "KillerName":   row.get("KillerName", ""),
            "destroyed_by": destroyed_by,
            "owned_by":     parsed["owned_by"]  if parsed else "Unknown",
            "lane":         parsed["lane"]       if parsed else "Unknown",
            "tier":         parsed["tier"]       if parsed else "Unknown",
            "label":        parsed["label"]      if parsed else "Unknown",
        })

    return pd.DataFrame(rows)


def enrich_inhibs(inhibs, players):
    """
    Inhibitor events don't carry a team field directly.
    Infer destroying team by looking for the nearest kill event before the inhib
    and using that killer's team — a reasonable proxy for who took the inhib.
    Falls back to 'Unknown' if no nearby kill exists.
    """
    inhibs = inhibs.copy()
    inhibs["destroyed_by"] = "Unknown"
    inhibs["lane"] = inhibs.get("InhibKilled", pd.Series(dtype=str)).apply(
        lambda x: _parse_inhib_lane(x) if isinstance(x, str) else "Unknown"
    )
    return inhibs


def _parse_inhib_lane(raw_name):
    # e.g. "Barracks_T1_L1" — L=top, C=mid, R=bot
    if not isinstance(raw_name, str):
        return "Unknown"
    parts = raw_name.upper()
    if "_L" in parts:
        return "Top"
    if "_C" in parts:
        return "Mid"
    if "_R" in parts:
        return "Bot"
    return "Unknown"


def sort_players_by_position(players_df):
    pos_col = "position" if "position" in players_df.columns else None
    if pos_col:
        cat = pd.CategoricalDtype(categories=POSITION_ORDER, ordered=True)
        players_df = players_df.copy()
        players_df[pos_col] = players_df[pos_col].astype(cat)
        players_df = players_df.sort_values([pos_col])
    return players_df


# ─────────────────────────────────────────────────────────────────────────────
# WIN CORRELATION ANALYSIS
# All functions below operate on a single match and return a dict of metrics.
# compute_all_correlations() aggregates them into one flat dict so the
# dashboard can display them and, later, accumulate them across many matches.
# ─────────────────────────────────────────────────────────────────────────────

def get_winner(metadata):
    """Return the winning team string from metadata, or None."""
    return metadata.get("match", {}).get("winner")


def first_blood_team(kills):
    """Return the team that got first blood, or None."""
    fb = kills[kills["EventTime"] == kills["EventTime"].min()]
    if fb.empty:
        return None
    killer = normalize_name(fb.iloc[0]["KillerName"])
    return fb.iloc[0].get("killer_team")  # set during preprocess enrichment


def _enrich_kills_with_team(kills, players):
    """Add killer_team and victim_team columns to kills dataframe."""
    team_map = dict(zip(players["normalized"], players["team"]))
    kills = kills.copy()
    kills["killer_team"] = kills["KillerName"].apply(
        lambda x: team_map.get(normalize_name(x))
    )
    kills["victim_team"] = kills["VictimName"].apply(
        lambda x: team_map.get(normalize_name(x))
    )
    return kills


def metric_first_blood(kills, winner):
    """Did the team that got first blood win?"""
    if winner is None or kills.empty:
        return None
    fb_kills = kills.sort_values("EventTime")
    if fb_kills.empty:
        return None
    fb_team = fb_kills.iloc[0].get("killer_team")
    if fb_team is None:
        return None
    return {"first_blood_team": fb_team, "winner": winner, "won": fb_team == winner}


def metric_first_turret(turrets, winner):
    """Did the team that destroyed the first turret win?"""
    if winner is None or turrets.empty:
        return None
    first = turrets.sort_values("EventTime").iloc[0]
    ft_team = first.get("destroyed_by")
    if ft_team in (None, "Unknown"):
        return None
    return {"first_turret_team": ft_team, "winner": winner, "won": ft_team == winner}


def metric_kill_lead_at_minute(kills, winner, minute=15):
    """
    Kill differential at a given minute mark.
    Returns order_kills, chaos_kills, leading_team, and whether the leader won.
    """
    if winner is None or kills.empty:
        return None
    early = kills[kills["EventTime"] <= minute * 60]
    order_k = (early["killer_team"] == "ORDER").sum()
    chaos_k = (early["killer_team"] == "CHAOS").sum()
    diff = int(order_k) - int(chaos_k)
    if diff == 0:
        leading_team = None
    else:
        leading_team = "ORDER" if diff > 0 else "CHAOS"
    return {
        "minute": minute,
        "order_kills": int(order_k),
        "chaos_kills": int(chaos_k),
        "kill_diff": diff,
        "leading_team": leading_team,
        "winner": winner,
        "leader_won": leading_team == winner if leading_team else None,
    }


def metric_teamfight_win_rate(fights, winner):
    """
    Of all detected teamfights, what fraction did each team win?
    Also: did the team that won more teamfights win the game?
    """
    if winner is None or not fights:
        return None
    order_wins = sum(1 for f in fights if f["winner"] == "ORDER")
    chaos_wins = sum(1 for f in fights if f["winner"] == "CHAOS")
    total = len(fights)
    tf_winner = None
    if order_wins > chaos_wins:
        tf_winner = "ORDER"
    elif chaos_wins > order_wins:
        tf_winner = "CHAOS"
    return {
        "total_fights": total,
        "order_wins": order_wins,
        "chaos_wins": chaos_wins,
        "order_win_rate": round(order_wins / total, 3) if total else None,
        "chaos_win_rate": round(chaos_wins / total, 3) if total else None,
        "teamfight_dominant_team": tf_winner,
        "winner": winner,
        "dominant_team_won": tf_winner == winner if tf_winner else None,
    }


def metric_first_teamfight(fights, winner):
    """Did the team that won the first teamfight win the game?"""
    if winner is None or not fights:
        return None
    first_fight_winner = fights[0]["winner"]
    return {
        "first_fight_winner": first_fight_winner,
        "first_fight_time_min": round(fights[0]["start"] / 60, 2),
        "game_winner": winner,
        "won": first_fight_winner == winner,
    }


def metric_turret_count(turrets, winner):
    """Did the team with more turrets destroyed win?"""
    if winner is None or turrets.empty:
        return None
    order_t = (turrets["destroyed_by"] == "ORDER").sum()
    chaos_t = (turrets["destroyed_by"] == "CHAOS").sum()
    if order_t > chaos_t:
        turret_leader = "ORDER"
    elif chaos_t > order_t:
        turret_leader = "CHAOS"
    else:
        turret_leader = None
    return {
        "order_turrets": int(order_t),
        "chaos_turrets": int(chaos_t),
        "turret_leader": turret_leader,
        "winner": winner,
        "leader_won": turret_leader == winner if turret_leader else None,
    }


def metric_early_level_lead(timeline, players, winner, minute=10):
    """
    Average team level at a given minute.
    Does the team with a higher average level win?
    """
    if winner is None or timeline.empty:
        return None
    snap = timeline[timeline["minute"] <= minute]
    if snap.empty:
        return None
    # Get the snapshot closest to the target minute per player
    closest = snap.groupby("player").apply(
        lambda g: g.loc[(g["minute"] - minute).abs().idxmin()]
    ).reset_index(drop=True)

    team_map = dict(zip(players["summonerName"], players["team"]))
    closest["team"] = closest["player"].map(team_map)

    avg = closest.groupby("team")["level"].mean()
    order_lvl = round(avg.get("ORDER", 0), 2)
    chaos_lvl = round(avg.get("CHAOS", 0), 2)
    diff = round(order_lvl - chaos_lvl, 2)

    if diff > 0:
        level_leader = "ORDER"
    elif diff < 0:
        level_leader = "CHAOS"
    else:
        level_leader = None

    return {
        "minute": minute,
        "order_avg_level": order_lvl,
        "chaos_avg_level": chaos_lvl,
        "level_diff": diff,
        "level_leader": level_leader,
        "winner": winner,
        "leader_won": level_leader == winner if level_leader else None,
    }


def metric_final_kill_differential(kills, winner):
    """Total kill differential at end of game."""
    if winner is None or kills.empty:
        return None
    order_k = (kills["killer_team"] == "ORDER").sum()
    chaos_k = (kills["killer_team"] == "CHAOS").sum()
    diff = int(order_k) - int(chaos_k)
    leader = "ORDER" if diff > 0 else ("CHAOS" if diff < 0 else None)
    return {
        "order_kills": int(order_k),
        "chaos_kills": int(chaos_k),
        "kill_diff": diff,
        "kill_leader": leader,
        "winner": winner,
        "leader_won": leader == winner if leader else None,
    }


def compute_all_correlations(metadata, players, kills_raw, turrets, timeline, fights):
    """
    Run every win correlation metric and return a single dict.
    Kills are enriched with team data here so all metrics share the same base.
    """
    winner = get_winner(metadata)
    kills = _enrich_kills_with_team(kills_raw, players)

    return {
        "winner": winner,
        "first_blood":          metric_first_blood(kills, winner),
        "first_turret":         metric_first_turret(turrets, winner),
        "kill_lead_15":         metric_kill_lead_at_minute(kills, winner, minute=15),
        "kill_lead_10":         metric_kill_lead_at_minute(kills, winner, minute=10),
        "first_teamfight":      metric_first_teamfight(fights, winner),
        "teamfight_win_rate":   metric_teamfight_win_rate(fights, winner),
        "turret_count":         metric_turret_count(turrets, winner),
        "level_lead_10":        metric_early_level_lead(timeline, players, winner, minute=10),
        "final_kill_diff":      metric_final_kill_differential(kills, winner),
    }


# ─────────────────────────────────────────────────────────────────────────────
# MINIMAP POSITION ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

# Summoner's Rift map dimensions — match minimap_tracker.py
MAP_WIDTH  = 14870
MAP_HEIGHT = 14870

# Named zones on Summoner's Rift for contextual labelling
# Each zone is (name, min_x, max_x, min_y, max_y)
MAP_ZONES = [
    ("Blue Base",     0,     2000,  0,     2000),
    ("Red Base",      12870, 14870, 12870, 14870),
    ("Dragon Pit",    8500,  10500, 3000,  5500),
    ("Baron Pit",     3500,  5500,  9500,  12000),
    ("Mid Lane",      5000,  10000, 5000,  10000),
    ("Top Lane",      0,     4000,  10000, 14870),
    ("Bot Lane",      10000, 14870, 0,     4000),
    ("Blue Jungle",   0,     7000,  0,     7000),
    ("Red Jungle",    7000,  14870, 7000,  14870),
]


def load_minimap_positions(folder):
    """
    Load minimap_positions.csv from a match folder.
    Returns a DataFrame or None if the file doesn't exist.
    """
    path = os.path.join(folder, "minimap_positions.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    return df


def preprocess_minimap(positions, metadata):
    """
    Clean and enrich minimap position data.

    - Convert wall-clock timestamps to game-relative seconds and minutes
    - Apply temporal deduplication: drop detections with no nearby match
      in the previous frame (filters out pings and transient false positives)
    - Cap detections per frame to 10 (one per champion)
    - Label map zones
    """
    if positions is None or positions.empty:
        return None

    df = positions.copy()

    # Convert wall-clock Unix timestamps to game-relative seconds
    # The first timestamp = game start (t=0)
    t0 = df["timestamp"].min()
    df["game_seconds"] = df["timestamp"] - t0
    df["minute"] = df["game_seconds"] / 60

    # ── Temporal consistency filter ───────────────────────────────────────────
    # Group by frame (same timestamp). For each detection, check if a detection
    # exists within 30 map units in the PREVIOUS frame. If not, it's likely a
    # ping or transient artefact — drop it.
    df = _temporal_filter(df, proximity=30)

    # ── Cap detections per frame to 10 ───────────────────────────────────────
    # If more than 10 are detected in a single frame, keep the 10 with the
    # highest confidence, or if all are 0.0 keep the first 5 per team.
    df = _cap_detections_per_frame(df)

    # ── Zone labelling ────────────────────────────────────────────────────────
    df["zone"] = df.apply(
        lambda r: _get_zone(r["map_x"], r["map_y"]), axis=1
    )

    return df


def _temporal_filter(df, proximity=30):
    """
    Drop detections that don't have a nearby detection in the previous frame.
    Champions persist across frames; pings appear once and vanish.
    """
    frames = sorted(df["timestamp"].unique())
    if len(frames) < 2:
        return df  # not enough frames to filter

    keep_indices = []
    prev_frame = None

    for ts in frames:
        curr = df[df["timestamp"] == ts]

        if prev_frame is None:
            # Always keep the first frame
            keep_indices.extend(curr.index.tolist())
            prev_frame = curr
            continue

        for idx, row in curr.iterrows():
            # Check if any detection in the previous frame is within proximity
            dists = ((prev_frame["map_x"] - row["map_x"]) ** 2 +
                     (prev_frame["map_y"] - row["map_y"]) ** 2) ** 0.5
            if (dists < proximity).any():
                keep_indices.append(idx)

        prev_frame = curr

    return df.loc[keep_indices].reset_index(drop=True)


def _cap_detections_per_frame(df, max_per_frame=10):
    """Keep at most max_per_frame detections per timestamp."""
    def cap_group(g):
        if len(g) <= max_per_frame:
            return g
        # Prefer higher confidence; if tied keep first 5 per team
        high_conf = g[g["confidence"] > 0]
        if len(high_conf) >= 1:
            return g.nlargest(max_per_frame, "confidence")
        order = g[g["team"] == "ORDER"].head(max_per_frame // 2)
        chaos = g[g["team"] == "CHAOS"].head(max_per_frame // 2)
        return pd.concat([order, chaos])

    return df.groupby("timestamp", group_keys=False).apply(cap_group).reset_index(drop=True)


def _get_zone(map_x, map_y):
    for name, x0, x1, y0, y1 in MAP_ZONES:
        if x0 <= map_x <= x1 and y0 <= map_y <= y1:
            return name
    return "Map"


def zone_presence(positions_df):
    """
    How much time (% of detections) did each team spend in each map zone?
    Returns a DataFrame with columns: team, zone, count, pct.
    """
    if positions_df is None or positions_df.empty:
        return pd.DataFrame()

    total = positions_df.groupby("team").size().rename("total")
    counts = positions_df.groupby(["team", "zone"]).size().rename("count").reset_index()
    counts = counts.merge(total.reset_index(), on="team")
    counts["pct"] = (counts["count"] / counts["total"] * 100).round(1)
    return counts.sort_values(["team", "count"], ascending=[True, False])


def movement_timeline(positions_df):
    """
    Average map_x and map_y per team per minute bucket.
    Useful for showing where each team was spending time across the game.
    """
    if positions_df is None or positions_df.empty:
        return pd.DataFrame()

    df = positions_df.copy()
    df["minute_bucket"] = df["minute"].apply(lambda m: int(m // 2) * 2)  # 2-min buckets
    agg = df.groupby(["minute_bucket", "team"]).agg(
        avg_x=("map_x", "mean"),
        avg_y=("map_y", "mean"),
        detections=("map_x", "count"),
    ).reset_index()
    return agg


# ─────────────────────────────────────────────────────────────────────────────
# SOLO KILL DETECTION
# Detects kills where ONE side was isolated — exactly one player from one team
# with no backup, while the other side had between 1 and max_enemy_side players.
#
# The key rule: min(killer_side, victim_side) == 1
# meaning at least one side was completely alone.
#
# Examples at max_enemy_side=2:
#   1v1 (no assisters)            → detected  isolated_team = victim team
#   1v2 (killer + 1 assister)     → detected  isolated_team = victim team
#   2v1 (victim had backup)       → detected  isolated_team = killer team
#   2v2 or more                   → NOT detected (both sides had backup)
#
# These are flagged for coaching regardless of outcome — both dying alone
# and killing someone alone represent coordination issues.
# ─────────────────────────────────────────────────────────────────────────────

def detect_solo_kills(kills, players, max_enemy_side=2):
    """
    Find kills where one side was completely isolated (exactly 1 player)
    while the other side had at most max_enemy_side players.

    max_enemy_side: maximum players allowed on the larger side (1-3).
    A value of 1 means strict 1v1 only.
    A value of 2 means 1v1 or 1v2 (one side always alone).
    A value of 3 means 1v1, 1v2, or 1v3.

    Returns a DataFrame with enriched columns including who was isolated.
    """
    if kills is None or kills.empty:
        return pd.DataFrame()

    team_map  = dict(zip(players["normalized"], players["team"]))
    champ_map = {}
    for _, p in players.iterrows():
        name = p.get("normalized", normalize_name(p.get("summonerName", "")))
        champ_map[name] = p.get("champion", p.get("summonerName", ""))

    rows = []
    for _, e in kills.iterrows():
        killer    = normalize_name(e.get("KillerName", ""))
        victim    = normalize_name(e.get("VictimName", ""))
        assisters = [normalize_name(a) for a in parse_assisters(e.get("Assisters"))]

        killer_team = team_map.get(killer)
        victim_team = team_map.get(victim)

        # Both must be real players on opposite teams
        if not killer_team or not victim_team:
            continue
        if killer_team == victim_team:
            continue

        # Count players on each side
        # Killer side: killer + any assisters on killer's team
        killer_side_players = [killer] + [
            a for a in assisters if team_map.get(a) == killer_team
        ]
        # Victim side: victim + any assisters on victim's team (rare but possible)
        victim_side_players = [victim] + [
            a for a in assisters if team_map.get(a) == victim_team
        ]

        killer_count = len(killer_side_players)
        victim_count = len(victim_side_players)

        # One side must be exactly 1 (isolated)
        isolated_count = min(killer_count, victim_count)
        larger_count   = max(killer_count, victim_count)

        if isolated_count != 1:
            continue  # Both sides had backup — this is a teamfight

        if larger_count > max_enemy_side:
            continue  # Other side too large for this threshold

        # Determine which side was isolated
        if killer_count == 1:
            isolated_team   = killer_team
            isolated_player = killer
            isolated_champ  = champ_map.get(killer, killer)
            other_team      = victim_team
            fight_type      = f"1v{victim_count}" if victim_count > 1 else "1v1"
        else:
            isolated_team   = victim_team
            isolated_player = victim
            isolated_champ  = champ_map.get(victim, victim)
            other_team      = killer_team
            fight_type      = f"1v{killer_count}" if killer_count > 1 else "1v1"

        rows.append({
            "EventTime":       e["EventTime"],
            "minute":          e["EventTime"] / 60,
            "killer":          e.get("KillerName", killer),
            "victim":          e.get("VictimName", victim),
            "killer_team":     killer_team,
            "victim_team":     victim_team,
            "killer_champ":    champ_map.get(killer, killer),
            "victim_champ":    champ_map.get(victim, victim),
            "killer_count":    killer_count,
            "victim_count":    victim_count,
            "fight_type":      fight_type,
            "isolated_team":   isolated_team,
            "isolated_player": isolated_player,
            "isolated_champ":  isolated_champ,
            "other_team":      other_team,
            "assisters":       ", ".join(assisters) if assisters else "—",
        })

    return pd.DataFrame(rows)


def solo_kill_summary(solo_kills, players):
    """
    Per-player summary: how many times they were isolated (died alone)
    and how many times they scored a kill while isolated or with backup.
    """
    if solo_kills is None or solo_kills.empty:
        return pd.DataFrame()

    from collections import defaultdict
    stats = defaultdict(lambda: {"isolated_deaths": 0, "isolated_kills": 0,
                                 "assisted_kills": 0})

    for _, row in solo_kills.iterrows():
        killer = row["killer"]
        victim = row["victim"]
        # Was the killer isolated?
        if row["killer_count"] == 1:
            stats[killer]["isolated_kills"] += 1
        else:
            stats[killer]["assisted_kills"] += 1
        # Victim was always the one who died — track isolated deaths
        if row["victim_count"] == 1:
            stats[victim]["isolated_deaths"] += 1

    rows = []
    for _, p in players.iterrows():
        name = p["summonerName"]
        s    = stats[name]
        rows.append({
            "player":           name,
            "team":             p["team"],
            "champion":         p.get("champion", name),
            "isolated_deaths":  s["isolated_deaths"],
            "isolated_kills":   s["isolated_kills"],
            "assisted_kills":   s["assisted_kills"],
            "total":            s["isolated_deaths"] + s["isolated_kills"] + s["assisted_kills"],
        })

    return pd.DataFrame(rows).sort_values("isolated_deaths", ascending=False)