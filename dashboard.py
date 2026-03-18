import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import os

from analysis import (
    load_match, preprocess,
    detect_teamfights, reconstruct_fights, compute_fight_breakdown,
    kill_participation, cs_per_minute, final_stats, sort_players_by_position,
    TEAM_COLORS, POSITION_ORDER,
)

st.set_page_config(
    page_title="Scrim Analysis",
    page_icon="⚔️",
    layout="wide",
)

# ── Styling ──────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; }
    .fight-card {
        background: #1e1e2e;
        border-radius: 10px;
        padding: 1rem 1.2rem;
        margin-bottom: 0.8rem;
        border-left: 4px solid #4A90D9;
    }
    .fight-card.chaos { border-left-color: #E05C5C; }
    .player-row {
        font-family: monospace;
        font-size: 0.9rem;
        padding: 2px 0;
    }
    .tag {
        display: inline-block;
        padding: 1px 8px;
        border-radius: 4px;
        font-size: 0.75rem;
        font-weight: 600;
        margin-right: 4px;
    }
    .tag-order { background: #1a3a5c; color: #4A90D9; }
    .tag-chaos { background: #3a1a1a; color: #E05C5C; }
    .tag-pos   { background: #2a2a3a; color: #aaa; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚔️ Scrim Analysis")
    st.markdown("---")

    folder = st.text_input(
        "Match folder path",
        placeholder="e.g. scrim_YYYY-MM-DD_HH-MM",
    )

    load_btn = st.button("Load Match", type="primary", use_container_width=True)

    st.markdown("---")
    st.markdown("**Teamfight Settings**")
    tf_window = st.slider("Kill window (seconds)", 5, 30, 15)
    tf_min_kills = st.slider("Min kills to qualify", 2, 6, 3)

# ── Load ──────────────────────────────────────────────────────────────────────
if not folder or not load_btn:
    st.markdown("## 👈 Enter a match folder path in the sidebar to get started")
    st.stop()

if not os.path.isdir(folder):
    st.error(f"Folder not found: `{folder}`")
    st.stop()

try:
    metadata, players, events, timeline = load_match(folder)
    timeline, kills, turrets, inhibs = preprocess(players, events, timeline)
except Exception as e:
    st.error(f"Failed to load match data: {e}")
    st.stop()

# Override teamfight settings from sidebar
import analysis
analysis.TEAMFIGHT_WINDOW = tf_window
analysis.TEAMFIGHT_MIN_KILLS = tf_min_kills

fights_raw = detect_teamfights(kills)
fights = reconstruct_fights(fights_raw, players)
fight_details = compute_fight_breakdown(fights_raw, players)
kp = kill_participation(kills, players)
cs_min = cs_per_minute(timeline)
final = final_stats(timeline, players)

order_players = sort_players_by_position(players[players["team"] == "ORDER"])
chaos_players = sort_players_by_position(players[players["team"] == "CHAOS"])

match_info = metadata.get("match", {})
game_duration = timeline["timestamp"].max()
game_duration_min = game_duration / 60

# ── Header ────────────────────────────────────────────────────────────────────
col_title, col_meta1, col_meta2, col_meta3 = st.columns([3, 1, 1, 1])
with col_title:
    st.title("Match Overview")
with col_meta1:
    st.metric("Duration", f"{game_duration_min:.1f} min")
with col_meta2:
    st.metric("Map Terrain", match_info.get("map_terrain", "—"))
with col_meta3:
    st.metric("Total Kills", len(kills))

st.markdown("---")

# ── Team compositions ─────────────────────────────────────────────────────────
st.subheader("Team Compositions")
col_order, col_chaos = st.columns(2)

def render_team_table(team_df, team_name):
    color = TEAM_COLORS[team_name]
    st.markdown(f"<span class='tag tag-{'order' if team_name=='ORDER' else 'chaos'}'>{team_name}</span>", unsafe_allow_html=True)

    rows = []
    for _, p in team_df.iterrows():
        rows.append({
            "Player": p.get("summonerName", ""),
            "Champion": p.get("champion", ""),
            "Position": p.get("position", ""),
            "Keystone": p.get("keystone", ""),
            "D1": p.get("summoner_spell_1", ""),
            "D2": p.get("summoner_spell_2", ""),
        })

    st.dataframe(
        pd.DataFrame(rows),
        hide_index=True,
        use_container_width=True,
    )

with col_order:
    render_team_table(order_players, "ORDER")

with col_chaos:
    render_team_table(chaos_players, "CHAOS")

st.markdown("---")

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_timeline, tab_vision, tab_events, tab_fights = st.tabs([
    "📈 Player Timeline",
    "👁️ Vision",
    "📋 Event Log",
    "⚔️ Teamfights",
])

# ── Tab: Player Timeline ──────────────────────────────────────────────────────
with tab_timeline:
    team_filter = st.radio("Team", ["Both", "ORDER", "CHAOS"], horizontal=True)
    metric = st.selectbox("Metric", ["cs", "kills", "deaths", "assists", "level"])

    tl = timeline.copy()
    if team_filter != "Both":
        tl = tl[tl["team"] == team_filter]

    # Add teamfight shading
    fig = px.line(
        tl,
        x="minute",
        y=metric,
        color="player",
        color_discrete_map={
            p: TEAM_COLORS.get(
                players.loc[players["summonerName"] == p, "team"].values[0]
                if p in players["summonerName"].values else "", "#888"
            )
            for p in tl["player"].unique()
        },
        labels={"minute": "Game Time (min)", metric: metric.upper()},
        title=f"{metric.upper()} over time",
    )

    # Shade teamfight windows
    for f in fights:
        fig.add_vrect(
            x0=f["start"] / 60,
            x1=f["end"] / 60,
            fillcolor="rgba(255,200,0,0.08)",
            line_width=0,
            annotation_text="TF",
            annotation_position="top left",
            annotation_font_size=9,
        )

    fig.update_layout(
        height=420,
        legend_title="Player",
        hovermode="x unified",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig, use_container_width=True)

    # Final stats table
    st.subheader("Final Stats")
    final_display = final[["player", "team", "champion", "position",
                             "kills", "deaths", "assists", "cs", "level"]].copy()
    final_display["KP%"] = final_display["player"].map(
        lambda p: f"{kp.get(p, 0)*100:.0f}%"
    )
    final_display["CS/min"] = final_display["player"].map(
        lambda p: f"{cs_min.get(p, 0):.2f}"
    )
    final_display = final_display.sort_values(["team", "position"])
    st.dataframe(final_display, hide_index=True, use_container_width=True)

# ── Tab: Vision ───────────────────────────────────────────────────────────────
with tab_vision:
    st.subheader("Ward Score Progression")

    fig_ward = px.line(
        timeline,
        x="minute",
        y="ward_score",
        color="player",
        labels={"minute": "Game Time (min)", "ward_score": "Ward Score"},
        title="Vision Score over Time",
    )
    fig_ward.update_layout(
        height=380,
        hovermode="x unified",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig_ward, use_container_width=True)

    # Final ward score bar chart
    st.subheader("Final Ward Score by Player")
    last_ward = timeline.sort_values("timestamp").groupby("player").tail(1)[
        ["player", "team", "ward_score"]
    ].sort_values("ward_score", ascending=True)

    fig_bar = px.bar(
        last_ward,
        x="ward_score",
        y="player",
        color="team",
        color_discrete_map=TEAM_COLORS,
        orientation="h",
        labels={"ward_score": "Final Ward Score", "player": ""},
        title="Final Vision Score",
    )
    fig_bar.update_layout(
        height=380,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig_bar, use_container_width=True)

# ── Tab: Event Log ────────────────────────────────────────────────────────────
with tab_events:
    st.subheader("Event Timeline")

    # Scatter timeline of all events
    event_types = []

    if not kills.empty:
        k = kills[["EventTime", "EventName", "KillerName", "VictimName"]].copy()
        k["label"] = k["KillerName"] + " killed " + k["VictimName"]
        k["category"] = "Kill"
        k["minute"] = k["EventTime"] / 60
        event_types.append(k[["minute", "category", "label"]])

    if not turrets.empty:
        t = turrets[["EventTime", "EventName"]].copy()
        t["label"] = "Turret destroyed"
        t["category"] = "Turret"
        t["minute"] = t["EventTime"] / 60
        event_types.append(t[["minute", "category", "label"]])

    if not inhibs.empty:
        i = inhibs[["EventTime", "EventName"]].copy()
        i["label"] = "Inhibitor destroyed"
        i["category"] = "Inhibitor"
        i["minute"] = i["EventTime"] / 60
        event_types.append(i[["minute", "category", "label"]])

    if event_types:
        all_events = pd.concat(event_types, ignore_index=True)

        cat_y = {"Kill": 1, "Turret": 2, "Inhibitor": 3}
        all_events["y"] = all_events["category"].map(cat_y)

        fig_ev = px.scatter(
            all_events,
            x="minute",
            y="category",
            color="category",
            hover_name="label",
            color_discrete_map={
                "Kill": "#E05C5C",
                "Turret": "#F0A500",
                "Inhibitor": "#7E57C2",
            },
            labels={"minute": "Game Time (min)", "category": ""},
            title="Events over Time",
        )

        # Add teamfight shading
        for f in fights:
            fig_ev.add_vrect(
                x0=f["start"] / 60,
                x1=f["end"] / 60,
                fillcolor="rgba(255,200,0,0.08)",
                line_width=0,
            )

        fig_ev.update_traces(marker_size=10)
        fig_ev.update_layout(
            height=280,
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            showlegend=True,
        )
        st.plotly_chart(fig_ev, use_container_width=True)

    # Raw event table with filter
    st.subheader("Raw Events")
    event_filter = st.multiselect(
        "Filter by event type",
        options=sorted(events["EventName"].unique()),
        default=["ChampionKill", "TurretKilled", "InhibKilled", "FirstBlood"],
    )
    filtered = events[events["EventName"].isin(event_filter)].copy()
    filtered["Time"] = (filtered["EventTime"] / 60).round(2).astype(str) + " min"
    st.dataframe(
        filtered.drop(columns=["minute"], errors="ignore"),
        hide_index=True,
        use_container_width=True,
    )

# ── Tab: Teamfights ───────────────────────────────────────────────────────────
with tab_fights:
    if not fights:
        st.info("No teamfights detected with current settings.")
        st.stop()

    st.subheader(f"{len(fights)} Teamfight(s) Detected")

    # Summary scatter
    fight_df = pd.DataFrame(fights)
    fight_df["minute"] = fight_df["start"] / 60
    fight_df["Fight"] = [f"Fight {i+1}" for i in range(len(fights))]

    fig_tf = px.scatter(
        fight_df,
        x="minute",
        y="kills",
        color="winner",
        size="kills",
        hover_name="Fight",
        hover_data={"duration": True, "minute": ":.1f"},
        color_discrete_map=TEAM_COLORS,
        labels={"minute": "Game Time (min)", "kills": "Kills in Fight"},
        title="Teamfight Map",
        size_max=30,
    )
    fig_tf.update_layout(
        height=300,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig_tf, use_container_width=True)

    st.markdown("---")
    st.subheader("Fight Breakdowns")

    for i, (fight, detail) in enumerate(zip(fights, fight_details), 1):
        winner = fight["winner"]
        start_min = fight["start"] / 60
        end_min = fight["end"] / 60
        border_class = "fight-card" if winner == "ORDER" else "fight-card chaos"
        winner_color = TEAM_COLORS[winner]

        with st.expander(
            f"Fight {i}  —  {start_min:.1f}–{end_min:.1f} min  |  "
            f"{fight['kills']} kills  |  Winner: {winner}",
            expanded=(i == 1),
        ):
            col_o, col_c = st.columns(2)

            def render_fight_team(col, team_key, team_label):
                with col:
                    color = TEAM_COLORS[team_label]
                    st.markdown(
                        f"<span class='tag tag-{'order' if team_label=='ORDER' else 'chaos'}'>"
                        f"{team_label}</span>",
                        unsafe_allow_html=True,
                    )
                    rows = detail[team_key]
                    if rows:
                        df = pd.DataFrame(rows)[["player", "k", "d", "a"]]
                        df.columns = ["Player", "K", "D", "A"]
                        st.dataframe(df, hide_index=True, use_container_width=True)
                    else:
                        st.caption("No participants recorded")

            render_fight_team(col_o, "order", "ORDER")
            render_fight_team(col_c, "chaos", "CHAOS")

            # Participants not mapped to either team (minions, turrets, etc.)
            all_mapped = (
                {p["player"] for p in detail["order"]} |
                {p["player"] for p in detail["chaos"]}
            )
            unmapped = [p for p in fight["participants"] if p not in all_mapped]
            if unmapped:
                st.caption(f"Other participants: {', '.join(unmapped)}")