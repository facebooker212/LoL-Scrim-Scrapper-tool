import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import os

from analysis import (
    load_match, preprocess,
    detect_teamfights, reconstruct_fights, compute_fight_breakdown,
    kill_participation, enrich_turrets, enrich_inhibs,
    compute_all_correlations,
    TEAM_COLORS,
)
import analysis

st.set_page_config(
    page_title="Scrim Analysis",
    page_icon="⚔️",
    layout="wide",
)

st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; }
    .section-header {
        font-size: 1.1rem;
        font-weight: 700;
        margin-bottom: 0.4rem;
        padding-bottom: 0.2rem;
        border-bottom: 1px solid #333;
    }
    .tag {
        display: inline-block;
        padding: 2px 10px;
        border-radius: 4px;
        font-size: 0.78rem;
        font-weight: 700;
        margin-right: 6px;
    }
    .tag-order { background: #1a3a5c; color: #4A90D9; }
    .tag-chaos { background: #3a1a1a; color: #E05C5C; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⚔️ Scrim Analysis")
    st.markdown("---")
    folder = st.text_input(
        "Match folder path",
        placeholder="e.g. scrim_2024-01-15_18-30",
    )
    load_btn = st.button("Load Match", type="primary", use_container_width=True)
    st.markdown("---")
    st.markdown("**Teamfight Detection**")
    tf_window    = st.slider("Kill window (seconds)", 5, 30, 15)
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
    timeline, kills, turrets_raw, inhibs_raw = preprocess(players, events, timeline)
except Exception as e:
    st.error(f"Failed to load match data: {e}")
    st.stop()

analysis.TEAMFIGHT_WINDOW    = tf_window
analysis.TEAMFIGHT_MIN_KILLS = tf_min_kills

fights_raw    = detect_teamfights(kills)
fights        = reconstruct_fights(fights_raw, players)
fight_details = compute_fight_breakdown(fights_raw, players)
kp            = kill_participation(kills, players)
turrets       = enrich_turrets(turrets_raw, players)
inhibs        = enrich_inhibs(inhibs_raw, players)

match_info        = metadata.get("match", {})
game_duration_min = timeline["timestamp"].max() / 60

# ── Header ────────────────────────────────────────────────────────────────────
c1, c2, c3, c4, c5 = st.columns([3, 1, 1, 1, 1])
with c1:
    st.title("Match Analysis")
with c2:
    st.metric("Duration", f"{game_duration_min:.1f} min")
with c3:
    st.metric("Total Kills", len(kills))
with c4:
    st.metric("Teamfights", len(fights))
with c5:
    st.metric("Turrets Down", len(turrets))

st.markdown("---")

tab_fights, tab_objectives, tab_correlations = st.tabs(["⚔️ Teamfights", "🏰 Objectives", "📊 Win Correlations"])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — TEAMFIGHTS
# ══════════════════════════════════════════════════════════════════════════════
with tab_fights:
    if not fights:
        st.info("No teamfights detected. Try lowering the thresholds in the sidebar.")
        st.stop()

    # Overview scatter
    fight_df = pd.DataFrame(fights)
    fight_df["minute"]     = (fight_df["start"] / 60).round(2)
    fight_df["Fight"]      = [f"Fight {i+1}" for i in range(len(fights))]
    fight_df["duration_s"] = fight_df["duration"].round(1)

    fig_scatter = px.scatter(
        fight_df,
        x="minute",
        y="kills",
        color="winner",
        size="kills",
        size_max=28,
        hover_name="Fight",
        hover_data={"minute": ":.1f", "duration_s": True, "kills": True, "winner": True},
        color_discrete_map=TEAM_COLORS,
        labels={"minute": "Game Time (min)", "kills": "Kills in Fight", "winner": "Winner"},
        title="Teamfight Overview — bubble size = kills",
    )
    fig_scatter.update_layout(
        height=300,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        hovermode="closest",
    )
    st.plotly_chart(fig_scatter, use_container_width=True)

    # Win summary
    order_wins = sum(1 for f in fights if f["winner"] == "ORDER")
    chaos_wins = sum(1 for f in fights if f["winner"] == "CHAOS")
    col_ow, col_cw = st.columns(2)
    with col_ow:
        st.metric("ORDER Teamfight Wins", order_wins)
    with col_cw:
        st.metric("CHAOS Teamfight Wins", chaos_wins)

    st.markdown("---")

    # Kill participation
    st.markdown("<div class='section-header'>Kill Participation</div>", unsafe_allow_html=True)
    kp_rows = []
    for _, p in players.iterrows():
        kp_rows.append({
            "Player":   p["summonerName"],
            "Team":     p["team"],
            "Champion": p.get("champion", ""),
            "Position": p.get("position", ""),
            "KP%":      f"{kp.get(p['summonerName'], 0) * 100:.0f}%",
        })
    kp_df = pd.DataFrame(kp_rows).sort_values(["Team", "Position"])
    st.dataframe(kp_df, hide_index=True, use_container_width=True)

    st.markdown("---")

    # Per-fight breakdowns
    st.markdown("<div class='section-header'>Fight Breakdowns</div>", unsafe_allow_html=True)

    for i, (fight, detail) in enumerate(zip(fights, fight_details), 1):
        winner    = fight["winner"]
        start_min = fight["start"] / 60
        end_min   = fight["end"] / 60

        label = (
            f"Fight {i}  ·  "
            f"{start_min:.1f} – {end_min:.1f} min  ·  "
            f"{fight['kills']} kills  ·  "
            f"{fight['duration']:.0f}s  ·  "
            f"Winner: {winner}"
        )

        with st.expander(label, expanded=(i == 1)):
            col_o, col_c = st.columns(2)

            def _render_side(col, team_key, team_label):
                with col:
                    tag_cls = "tag-order" if team_label == "ORDER" else "tag-chaos"
                    st.markdown(
                        f"<span class='tag {tag_cls}'>{team_label}</span>",
                        unsafe_allow_html=True,
                    )
                    rows = detail[team_key]
                    if rows:
                        df = pd.DataFrame(rows)[["player", "k", "d", "a"]]
                        df.columns = ["Player", "K", "D", "A"]
                        st.dataframe(df, hide_index=True, use_container_width=True)
                    else:
                        st.caption("No participants recorded")

            _render_side(col_o, "order", "ORDER")
            _render_side(col_c, "chaos", "CHAOS")

            mapped = (
                {p["player"] for p in detail["order"]} |
                {p["player"] for p in detail["chaos"]}
            )
            unmapped = [p for p in fight["participants"] if p not in mapped]
            if unmapped:
                st.caption(f"Other: {', '.join(unmapped)}")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — OBJECTIVES
# ══════════════════════════════════════════════════════════════════════════════
with tab_objectives:

    st.markdown("<div class='section-header'>Objective Timeline</div>", unsafe_allow_html=True)

    fig_tl = go.Figure()

    # Faint kill markers for context
    if not kills.empty:
        fig_tl.add_trace(go.Scatter(
            x=kills["EventTime"] / 60,
            y=["Kill"] * len(kills),
            mode="markers",
            marker=dict(size=5, color="rgba(150,150,150,0.2)"),
            name="Kill",
            hovertext=kills["KillerName"].astype(str) + " killed " + kills["VictimName"].astype(str),
            hoverinfo="text",
        ))

    # Teamfight shading
    for f in fights:
        fig_tl.add_vrect(
            x0=f["start"] / 60,
            x1=f["end"] / 60,
            fillcolor="rgba(255,220,0,0.07)",
            line_width=0,
        )

    # Turret markers
    if not turrets.empty:
        for tier in turrets["tier"].unique():
            subset = turrets[turrets["tier"] == tier]
            fig_tl.add_trace(go.Scatter(
                x=subset["EventTime"] / 60,
                y=[f"Turret ({tier})"] * len(subset),
                mode="markers+text",
                marker=dict(
                    size=14,
                    color=subset["destroyed_by"].map(TEAM_COLORS).fillna("#888"),
                    symbol="square",
                    line=dict(width=1, color="#fff"),
                ),
                text=subset["lane"],
                textposition="top center",
                textfont=dict(size=9),
                name=f"Turret ({tier})",
                hovertext=(
                    subset["destroyed_by"] + " destroyed "
                    + subset["lane"] + " " + subset["tier"] + " turret"
                ),
                hoverinfo="text",
            ))

    # Inhibitor markers
    if not inhibs.empty:
        fig_tl.add_trace(go.Scatter(
            x=inhibs["EventTime"] / 60,
            y=["Inhibitor"] * len(inhibs),
            mode="markers",
            marker=dict(
                size=16,
                color=inhibs.get("destroyed_by", pd.Series(["Unknown"] * len(inhibs))).map(TEAM_COLORS).fillna("#7E57C2"),
                symbol="diamond",
                line=dict(width=1, color="#fff"),
            ),
            name="Inhibitor",
            hovertext="Inhibitor destroyed",
            hoverinfo="text",
        ))

    fig_tl.update_layout(
        height=360,
        xaxis_title="Game Time (min)",
        yaxis_title="",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        hovermode="closest",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    st.plotly_chart(fig_tl, use_container_width=True)
    st.caption("🟡 Shaded bands = teamfight windows  ·  Marker color = destroying team (ORDER=blue, CHAOS=red)")

    st.markdown("---")

    # Turret table
    st.markdown("<div class='section-header'>Turrets Destroyed</div>", unsafe_allow_html=True)

    if not turrets.empty:
        order_t = (turrets["destroyed_by"] == "ORDER").sum()
        chaos_t = (turrets["destroyed_by"] == "CHAOS").sum()
        col_ot, col_ct = st.columns(2)
        with col_ot:
            st.metric("Turrets by ORDER", order_t)
        with col_ct:
            st.metric("Turrets by CHAOS", chaos_t)

        turret_display = turrets[["EventTime", "destroyed_by", "lane", "tier"]].copy()
        turret_display["Time"] = (turret_display["EventTime"] / 60).round(2).astype(str) + " min"
        turret_display = turret_display.rename(columns={
            "destroyed_by": "Destroyed By",
            "lane": "Lane",
            "tier": "Tier",
        })[["Time", "Destroyed By", "Lane", "Tier"]]
        st.dataframe(turret_display, hide_index=True, use_container_width=True)
    else:
        st.info("No turret events recorded.")

    st.markdown("---")

    # Inhibitor table
    st.markdown("<div class='section-header'>Inhibitors</div>", unsafe_allow_html=True)

    if not inhibs.empty:
        inhib_display = inhibs.copy()
        inhib_display["Time"] = (inhib_display["EventTime"] / 60).round(2).astype(str) + " min"
        display_cols = [c for c in ["Time", "EventName", "destroyed_by", "lane"] if c in inhib_display.columns]
        inhib_display = inhib_display[display_cols].rename(columns={
            "destroyed_by": "Destroyed By",
            "lane": "Lane",
            "EventName": "Event",
        })
        st.dataframe(inhib_display, hide_index=True, use_container_width=True)
    else:
        st.info("No inhibitor events recorded.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — WIN CORRELATIONS
# ══════════════════════════════════════════════════════════════════════════════
with tab_correlations:

    corr = compute_all_correlations(metadata, players, kills, turrets, timeline, fights)
    winner = corr["winner"]

    if winner is None:
        st.warning(
            "⚠️ Winner not found in metadata.json — "
            "make sure you're running the latest main.py which captures the winner at game end. "
            "All metrics below require a known winner to evaluate."
        )
    else:
        st.success(f"✅ Winner: **{winner}**")

    st.markdown("---")

    def result_badge(won):
        if won is True:
            return "✅ Yes"
        if won is False:
            return "❌ No"
        return "— N/A"

    def team_tag(team):
        if team == "ORDER":
            return f"<span class='tag tag-order'>ORDER</span>"
        if team == "CHAOS":
            return f"<span class='tag tag-chaos'>CHAOS</span>"
        return "—"

    # ── First Blood ───────────────────────────────────────────────────────────
    st.markdown("<div class='section-header'>First Blood</div>", unsafe_allow_html=True)
    fb = corr["first_blood"]
    if fb:
        c1, c2, c3 = st.columns(3)
        c1.markdown(f"**Got First Blood**<br>{team_tag(fb['first_blood_team'])}", unsafe_allow_html=True)
        c2.markdown(f"**Game Winner**<br>{team_tag(fb['winner'])}", unsafe_allow_html=True)
        c3.metric("First Blood Team Won", result_badge(fb["won"]))
    else:
        st.caption("Not enough data.")

    st.markdown("---")

    # ── First Turret ──────────────────────────────────────────────────────────
    st.markdown("<div class='section-header'>First Turret</div>", unsafe_allow_html=True)
    ft = corr["first_turret"]
    if ft:
        c1, c2, c3 = st.columns(3)
        c1.markdown(f"**Destroyed First Turret**<br>{team_tag(ft['first_turret_team'])}", unsafe_allow_html=True)
        c2.markdown(f"**Game Winner**<br>{team_tag(ft['winner'])}", unsafe_allow_html=True)
        c3.metric("First Turret Team Won", result_badge(ft["won"]))
    else:
        st.caption("Not enough data.")

    st.markdown("---")

    # ── Turret Count ──────────────────────────────────────────────────────────
    st.markdown("<div class='section-header'>Total Turrets Destroyed</div>", unsafe_allow_html=True)
    tc = corr["turret_count"]
    if tc:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("ORDER Turrets", tc["order_turrets"])
        c2.metric("CHAOS Turrets", tc["chaos_turrets"])
        c3.markdown(f"**Turret Leader**<br>{team_tag(tc['turret_leader'])}", unsafe_allow_html=True)
        c4.metric("Turret Leader Won", result_badge(tc["leader_won"]))
    else:
        st.caption("Not enough data.")

    st.markdown("---")

    # ── Kill Lead at 10 and 15 min ────────────────────────────────────────────
    st.markdown("<div class='section-header'>Kill Lead</div>", unsafe_allow_html=True)
    for key, label in [("kill_lead_10", "at 10 min"), ("kill_lead_15", "at 15 min")]:
        kl = corr[key]
        st.markdown(f"**{label}**")
        if kl:
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("ORDER Kills", kl["order_kills"])
            c2.metric("CHAOS Kills", kl["chaos_kills"])
            c3.metric("Diff (ORDER - CHAOS)", f"{kl['kill_diff']:+d}")
            c4.markdown(f"**Leading Team**<br>{team_tag(kl['leading_team'])}", unsafe_allow_html=True)
            c5.metric("Leader Won", result_badge(kl["leader_won"]))
        else:
            st.caption("Not enough data.")

    st.markdown("---")

    # ── Final Kill Differential ───────────────────────────────────────────────
    st.markdown("<div class='section-header'>Final Kill Differential</div>", unsafe_allow_html=True)
    fk = corr["final_kill_diff"]
    if fk:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("ORDER Kills", fk["order_kills"])
        c2.metric("CHAOS Kills", fk["chaos_kills"])
        c3.metric("Diff (ORDER - CHAOS)", f"{fk['kill_diff']:+d}")
        c4.markdown(f"**Kill Leader**<br>{team_tag(fk['kill_leader'])}", unsafe_allow_html=True)
        c5.metric("Kill Leader Won", result_badge(fk["leader_won"]))
    else:
        st.caption("Not enough data.")

    st.markdown("---")

    # ── Level Lead at 10 min ──────────────────────────────────────────────────
    st.markdown("<div class='section-header'>Average Level at 10 min</div>", unsafe_allow_html=True)
    ll = corr["level_lead_10"]
    if ll:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("ORDER Avg Level", ll["order_avg_level"])
        c2.metric("CHAOS Avg Level", ll["chaos_avg_level"])
        c3.metric("Diff (ORDER - CHAOS)", f"{ll['level_diff']:+.2f}")
        c4.markdown(f"**Level Leader**<br>{team_tag(ll['level_leader'])}", unsafe_allow_html=True)
        c5.metric("Level Leader Won", result_badge(ll["leader_won"]))
    else:
        st.caption("Not enough data.")

    st.markdown("---")

    # ── Teamfight Win Rate ────────────────────────────────────────────────────
    st.markdown("<div class='section-header'>Teamfight Win Rate</div>", unsafe_allow_html=True)
    tf = corr["teamfight_win_rate"]
    if tf:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Fights", tf["total_fights"])
        c2.metric("ORDER Wins", f"{tf['order_wins']} ({tf['order_win_rate']*100:.0f}%)")
        c3.metric("CHAOS Wins", f"{tf['chaos_wins']} ({tf['chaos_win_rate']*100:.0f}%)")
        c4.markdown(f"**Dominant Team**<br>{team_tag(tf['teamfight_dominant_team'])}", unsafe_allow_html=True)
        c5.metric("Dominant Team Won", result_badge(tf["dominant_team_won"]))

        # Bar chart
        tf_bar = pd.DataFrame([
            {"Team": "ORDER", "Fights Won": tf["order_wins"]},
            {"Team": "CHAOS", "Fights Won": tf["chaos_wins"]},
        ])
        fig_tf = px.bar(
            tf_bar, x="Team", y="Fights Won",
            color="Team", color_discrete_map=TEAM_COLORS,
            title="Teamfight Wins by Team",
        )
        fig_tf.update_layout(
            height=250, showlegend=False,
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig_tf, use_container_width=True)
    else:
        st.caption("Not enough data.")

    st.markdown("---")

    # ── First Teamfight ───────────────────────────────────────────────────────
    st.markdown("<div class='section-header'>First Teamfight</div>", unsafe_allow_html=True)
    ftf = corr["first_teamfight"]
    if ftf:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("First Fight Time", f"{ftf['first_fight_time_min']:.1f} min")
        c2.markdown(f"**First Fight Winner**<br>{team_tag(ftf['first_fight_winner'])}", unsafe_allow_html=True)
        c3.markdown(f"**Game Winner**<br>{team_tag(ftf['game_winner'])}", unsafe_allow_html=True)
        c4.metric("First Fight Winner Won Game", result_badge(ftf["won"]))
    else:
        st.caption("Not enough data.")

    st.markdown("---")
    st.caption(
        "📌 These metrics reflect a single match. "
        "Win correlation percentages become meaningful once you have 10+ matches recorded."
    )