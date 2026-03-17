import pandas as pd
import matplotlib.pyplot as plt
import json
import os
import sys
import ast
from collections import defaultdict

TEAMFIGHT_WINDOW = 15
TEAMFIGHT_MIN_KILLS = 3


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

    return metadata, players, events, timeline


def preprocess(players, events, timeline):

    timeline["minute"] = timeline["timestamp"] / 60
    events["minute"] = events["EventTime"] / 60

    timeline = timeline.merge(
        players,
        left_on="player",
        right_on="summonerName",
        how="left"
    )

    kills = events[events["EventName"] == "ChampionKill"].copy()

    return timeline, kills


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

            assisters = parse_assisters(e.get("Assisters"))

            for a in assisters:
                participants.add(normalize_name(a))

            if killer in player_team:

                if player_team[killer] == "ORDER":
                    order_kills += 1
                else:
                    chaos_kills += 1

        winner = "ORDER" if order_kills > chaos_kills else "CHAOS"

        results.append({
            "start": start,
            "end": end,
            "kills": len(fight),
            "winner": winner,
            "participants": list(participants)
        })

    return results


def compute_fight_breakdown(fights, players):

    player_team = dict(zip(players["normalized"], players["team"]))

    fight_details = []

    for fight in fights:

        stats = defaultdict(lambda: {"k":0,"d":0,"a":0})

        for e in fight:

            killer = normalize_name(e["KillerName"])
            victim = normalize_name(e["VictimName"])

            stats[killer]["k"] += 1
            stats[victim]["d"] += 1

            assisters = parse_assisters(e.get("Assisters"))

            for a in assisters:
                a = normalize_name(a)
                stats[a]["a"] += 1

        order_players = []
        chaos_players = []

        for player, s in stats.items():

            team = player_team.get(player)

            entry = (player, s["k"], s["d"], s["a"])

            if team == "ORDER":
                order_players.append(entry)
            elif team == "CHAOS":
                chaos_players.append(entry)

        fight_details.append({
            "start": fight[0]["EventTime"],
            "end": fight[-1]["EventTime"],
            "order": order_players,
            "chaos": chaos_players
        })

    return fight_details


def objective_events(events):

    turrets = events[events["EventName"] == "TurretKilled"]
    inhibs = events[events["EventName"] == "InhibKilled"]

    return turrets, inhibs


def kill_participation(kills, players):

    # Use normalized names everywhere
    team_map = dict(zip(players["normalized"], players["team"]))

    team_kills = defaultdict(int)
    kp = defaultdict(int)

    for _, row in kills.iterrows():

        killer = normalize_name(row["KillerName"])
        victim = normalize_name(row["VictimName"])

        if killer not in team_map:
            continue

        team = team_map[killer]

        team_kills[team] += 1
        kp[killer] += 1

        assisters = parse_assisters(row.get("Assisters"))

        for a in assisters:

            a = normalize_name(a)

            if a in team_map:
                kp[a] += 1

    results = {}

    for _, player in players.iterrows():

        name = player["normalized"]
        team = player["team"]

        if team_kills[team] == 0:
            results[player["summonerName"]] = 0
        else:
            results[player["summonerName"]] = kp[name] / team_kills[team]

    return results


def cs_per_minute(timeline):

    last_frame = timeline.sort_values("timestamp").groupby("player").tail(1)

    cs_min = {}

    for _, r in last_frame.iterrows():

        minutes = r["timestamp"] / 60
        cs = r["cs"]

        if minutes == 0:
            cs_min[r["player"]] = 0
        else:
            cs_min[r["player"]] = cs / minutes

    return cs_min


def plot_dashboard(timeline, fights, turrets, inhibs):

    fig, axs = plt.subplots(3, 1, figsize=(12, 12))

    # Level progression
    for p in timeline["player"].unique():
        p_data = timeline[timeline["player"] == p]
        axs[0].plot(p_data["minute"], p_data["level"])

    axs[0].set_title("Level Progression")
    axs[0].set_xlabel("Minute")
    axs[0].set_ylabel("Level")

    # Teamfight timeline
    fight_times = [f["start"]/60 for f in fights]
    fight_sizes = [f["kills"] for f in fights]

    axs[1].scatter(fight_times, fight_sizes)

    axs[1].set_title("Teamfight Timeline")
    axs[1].set_xlabel("Minute")
    axs[1].set_ylabel("Kills in Fight")

    # Objective timeline
    axs[2].scatter(turrets["EventTime"]/60, [1]*len(turrets), label="Turrets")
    axs[2].scatter(inhibs["EventTime"]/60, [2]*len(inhibs), label="Inhibitors")

    axs[2].set_title("Objective Timeline")
    axs[2].set_yticks([1,2])
    axs[2].set_yticklabels(["Turret","Inhib"])
    axs[2].legend()

    plt.tight_layout()
    plt.show()


def print_summary(fights, kp, cs_min):

    print("\nMATCH SUMMARY")
    print("==============")

    print("\nTEAMFIGHTS")
    print("----------------")

    for i, f in enumerate(fights, 1):

        start = f["start"]/60
        end = f["end"]/60

        print(f"\nFight {i}")
        print(f"Time: {start:.2f} – {end:.2f}")
        print(f"Kills: {f['kills']}")
        print(f"Winner: {f['winner']}")

        print("Participants:")
        for p in f["participants"]:
            print("  ", p)

    print("\nKILL PARTICIPATION")
    print("-------------------")

    for p, v in sorted(kp.items(), key=lambda x: x[1], reverse=True):
        print(f"{p:25} {v*100:.1f}%")

    print("\nCS PER MINUTE")
    print("----------------")

    for p, v in sorted(cs_min.items(), key=lambda x: x[1], reverse=True):
        print(f"{p:25} {v:.2f}")


def print_fight_breakdowns(details):

    print("\nFIGHT BREAKDOWNS")
    print("--------------------")

    for i, f in enumerate(details,1):

        start = f["start"]/60
        end = f["end"]/60

        print(f"\nFIGHT {i} — {start:.2f}-{end:.2f}")

        print("\nORDER")

        for p,k,d,a in f["order"]:
            print(f"{p:20} {k}/{d}/{a}")

        print("\nCHAOS")

        for p,k,d,a in f["chaos"]:
            print(f"{p:20} {k}/{d}/{a}")


def main():

    if len(sys.argv) < 2:
        print("Usage: python match_analysis_v2.py <match_folder>")
        return

    folder = sys.argv[1]

    metadata, players, events, timeline = load_match(folder)

    timeline, kills = preprocess(players, events, timeline)

    fights = detect_teamfights(kills)

    fight_results = reconstruct_fights(fights, players)

    fight_details = compute_fight_breakdown(fights, players)

    turrets, inhibs = objective_events(events)

    kp = kill_participation(kills, players)

    cs_min = cs_per_minute(timeline)

    print_summary(fight_results, kp, cs_min)

    print_fight_breakdowns(fight_details)

    plot_dashboard(timeline, fight_results, turrets, inhibs)


if __name__ == "__main__":
    main()