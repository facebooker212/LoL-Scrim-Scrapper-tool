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


def sort_players_by_position(players_df):
    pos_col = "position" if "position" in players_df.columns else None
    if pos_col:
        cat = pd.CategoricalDtype(categories=POSITION_ORDER, ordered=True)
        players_df = players_df.copy()
        players_df[pos_col] = players_df[pos_col].astype(cat)
        players_df = players_df.sort_values([pos_col])
    return players_df