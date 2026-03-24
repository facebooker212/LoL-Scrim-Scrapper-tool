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
    load_minimap_positions, preprocess_minimap,
    zone_presence, movement_timeline,
    detect_solo_kills, solo_kill_summary,
    TEAM_COLORS, MAP_WIDTH, MAP_HEIGHT,
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

# Load minimap positions if available
minimap_raw = load_minimap_positions(folder)
minimap_df  = preprocess_minimap(minimap_raw, metadata) if minimap_raw is not None else None

fights_raw    = detect_teamfights(kills)
# solo_kills computed later with slider value — initialise with default
solo_kills    = detect_solo_kills(kills, players, max_enemy_side=2)
solo_summary  = solo_kill_summary(solo_kills, players)
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

tab_fights, tab_objectives, tab_correlations, tab_minimap, tab_solo = st.tabs(["⚔️ Teamfights", "🏰 Objectives", "📊 Win Correlations", "🗺️ Player Movement", "⚠️ Solo Fights"])

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

# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — PLAYER MOVEMENT
# ══════════════════════════════════════════════════════════════════════════════
with tab_minimap:

    if minimap_df is None or minimap_df.empty:
        st.info(
            "No minimap position data found for this match. "
            "Make sure minimap_tracker.py ran alongside main.py and that "
            "minimap_positions.csv is present in the match folder."
        )
        st.stop()

    # ── Build champion list ───────────────────────────────────────────────────
    # Only champions with identified detections get path lines
    mm = minimap_df.copy()
    mm["display_y"] = MAP_HEIGHT - mm["map_y"]

    known_df = mm[mm["champion"] != "unknown"].copy()
    all_champs = sorted(known_df["champion"].unique()) if not known_df.empty else []

    # Build a stable color per champion — ORDER blue hues, CHAOS red hues
    import colorsys
    order_champs_all = sorted([
        c for c in all_champs
        if not known_df[known_df["champion"] == c].empty and
        known_df[known_df["champion"] == c]["team"].iloc[0] == "ORDER"
    ])
    chaos_champs_all = sorted([
        c for c in all_champs
        if not known_df[known_df["champion"] == c].empty and
        known_df[known_df["champion"] == c]["team"].iloc[0] == "CHAOS"
    ])

    def make_champ_colors(champ_list, team):
        colors = {}
        n = max(len(champ_list), 1)
        for i, c in enumerate(champ_list):
            if team == "ORDER":
                h = 0.52 + 0.14 * (i / n)
            else:
                h = 0.98 + 0.10 * (i / n)
                h = h % 1.0
            r, g, b = colorsys.hsv_to_rgb(h, 0.85, 0.95)
            colors[c] = f"rgb({int(r*255)},{int(g*255)},{int(b*255)})"
        return colors

    champ_colors = {}
    champ_colors.update(make_champ_colors(order_champs_all, "ORDER"))
    champ_colors.update(make_champ_colors(chaos_champs_all, "CHAOS"))

    st.markdown(
        f"**{len(mm):,} total detections**  ·  "
        f"**{len(known_df):,} identified**  ·  "
        f"**{len(all_champs)} champions**  ·  "
        f"Game: {mm['minute'].max():.1f} min"
    )

    st.markdown("---")

    # ── Champion selector ─────────────────────────────────────────────────────
    st.markdown("<div class='section-header'>Champion Path Map</div>",
                unsafe_allow_html=True)
    st.caption(
        "Select one or more champions to draw their movement path. "
        "Start with none selected to see an empty map, then add players one by one."
    )

    # Two-column selector: ORDER left, CHAOS right
    col_sel_o, col_sel_c = st.columns(2)
    with col_sel_o:
        st.markdown("<span class='tag tag-order'>ORDER</span>", unsafe_allow_html=True)
        sel_order = st.multiselect(
            "ORDER champions",
            options=order_champs_all,
            default=[],
            key="path_order",
            label_visibility="collapsed",
        )
    with col_sel_c:
        st.markdown("<span class='tag tag-chaos'>CHAOS</span>", unsafe_allow_html=True)
        sel_chaos = st.multiselect(
            "CHAOS champions",
            options=chaos_champs_all,
            default=[],
            key="path_chaos",
            label_visibility="collapsed",
        )

    selected = sel_order + sel_chaos

    # ── Path map ──────────────────────────────────────────────────────────────
    fig_map = go.Figure()

    if not selected:
        # Empty map with just axis setup — prompt user to select
        fig_map.add_annotation(
            text="Select champions above to draw their paths",
            xref="paper", yref="paper",
            x=0.5, y=0.5,
            showarrow=False,
            font=dict(size=16, color="#888"),
        )
    else:
        for champ in selected:
            c_df = known_df[known_df["champion"] == champ].sort_values("minute")
            if c_df.empty:
                continue
            color = champ_colors.get(champ, "#aaa")

            # Full path line
            fig_map.add_trace(go.Scatter(
                x=c_df["map_x"],
                y=c_df["display_y"],
                mode="lines",
                name=champ,
                line=dict(color=color, width=2),
                opacity=0.75,
                hoverinfo="skip",
                showlegend=True,
            ))

            # Detection dots on top of the line
            fig_map.add_trace(go.Scatter(
                x=c_df["map_x"],
                y=c_df["display_y"],
                mode="markers",
                name=champ,
                marker=dict(size=5, color=color),
                hovertemplate=(
                    f"<b>{champ}</b><br>"
                    "Time: %{customdata:.1f} min<br>"
                    "Zone: %{text}<br>"
                    "<extra></extra>"
                ),
                customdata=c_df["minute"].values,
                text=c_df["zone"].values,
                showlegend=False,
            ))

            # Start marker (open circle) and end marker (star)
            fig_map.add_trace(go.Scatter(
                x=[c_df.iloc[0]["map_x"], c_df.iloc[-1]["map_x"]],
                y=[c_df.iloc[0]["display_y"], c_df.iloc[-1]["display_y"]],
                mode="markers",
                marker=dict(
                    size=[12, 14],
                    color=color,
                    symbol=["circle-open", "star"],
                    line=dict(width=2, color=color),
                ),
                showlegend=False,
                hoverinfo="skip",
            ))

    fig_map.update_layout(
        height=600,
        xaxis=dict(range=[0, MAP_WIDTH], showgrid=False, zeroline=False,
                   showticklabels=False, title=""),
        yaxis=dict(range=[0, MAP_HEIGHT], showgrid=False, zeroline=False,
                   showticklabels=False, title="",
                   scaleanchor="x", scaleratio=1),
        plot_bgcolor="rgba(15,25,15,1)",
        paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(
            orientation="v",
            x=1.01, y=1,
            bgcolor="rgba(0,0,0,0.4)",
            font=dict(size=11),
        ),
        hovermode="closest",
    )
    st.plotly_chart(fig_map, use_container_width=True)
    st.caption("○ = path start  ★ = path end  · Lines connect detections chronologically")

    st.markdown("---")

    # ── Individual heatmaps ───────────────────────────────────────────────────
    if selected:
        st.markdown("<div class='section-header'>Individual Position Heatmaps</div>",
                    unsafe_allow_html=True)
        st.caption("Where each selected champion spent most of their time across the game.")

        # Render in rows of 2
        for i in range(0, len(selected), 2):
            cols = st.columns(2)
            for j, champ in enumerate(selected[i:i+2]):
                c_df = known_df[known_df["champion"] == champ]
                if c_df.empty:
                    continue
                color = champ_colors.get(champ, "#aaa")
                team  = c_df["team"].iloc[0]

                with cols[j]:
                    tag_cls = "tag-order" if team == "ORDER" else "tag-chaos"
                    st.markdown(
                        f"<span class='tag {tag_cls}'>{champ}</span>  "
                        f"<span style='color:#888;font-size:0.8rem'>"
                        f"{len(c_df)} detections</span>",
                        unsafe_allow_html=True,
                    )

                    fig_h = go.Figure()
                    fig_h.add_trace(go.Histogram2dContour(
                        x=c_df["map_x"],
                        y=c_df["display_y"],
                        colorscale=[
                            [0, "rgba(0,0,0,0)"],
                            [0.3, color.replace("rgb", "rgba").replace(")", ",0.3)")],
                            [1, color],
                        ],
                        showscale=False,
                        ncontours=10,
                        contours=dict(showlines=False),
                    ))
                    # Scatter dots
                    fig_h.add_trace(go.Scatter(
                        x=c_df["map_x"],
                        y=c_df["display_y"],
                        mode="markers",
                        marker=dict(size=3, color=color, opacity=0.3),
                        showlegend=False,
                        hoverinfo="skip",
                    ))
                    fig_h.update_layout(
                        height=300,
                        xaxis=dict(range=[0, MAP_WIDTH], showgrid=False,
                                   zeroline=False, showticklabels=False, title=""),
                        yaxis=dict(range=[0, MAP_HEIGHT], showgrid=False,
                                   zeroline=False, showticklabels=False,
                                   title="", scaleanchor="x", scaleratio=1),
                        plot_bgcolor="rgba(15,25,15,1)",
                        paper_bgcolor="rgba(0,0,0,0)",
                        margin=dict(l=0, r=0, t=5, b=0),
                    )
                    st.plotly_chart(fig_h, use_container_width=True)
    else:
        st.info("Select champions above to see their individual heatmaps.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — SOLO FIGHTS
# ══════════════════════════════════════════════════════════════════════════════
with tab_solo:

    st.markdown("""
    Detects kills where **one side was completely isolated** — exactly one player
    with no backup — while the other side had up to the configured maximum.
    A 1v1, 1v2, or 1v3 all count as long as one side was alone.
    Both outcomes are shown since dying alone and killing while alone both
    represent coordination issues.
    """)

    # ── Slider ────────────────────────────────────────────────────────────────
    col_s1, col_s2 = st.columns([2, 1])
    with col_s1:
        max_enemy = st.slider(
            "Max players on the larger side",
            min_value=1,
            max_value=3,
            value=2,
            help=(
                "1 = strict 1v1 only  |  "
                "2 = 1v1 and 1v2  |  "
                "3 = 1v1, 1v2, and 1v3. "
                "One side must always be completely alone."
            ),
            key="solo_max_enemy",
        )
    with col_s2:
        st.markdown(
            f"<br><span style='color:#aaa;font-size:0.9rem'>"
            f"Showing fights where one side had exactly 1 player "
            f"and the other had 1–{max_enemy}</span>",
            unsafe_allow_html=True,
        )

    # Recompute with current slider value
    solo_kills   = detect_solo_kills(kills, players, max_enemy_side=max_enemy)
    solo_summary = solo_kill_summary(solo_kills, players)

    if solo_kills.empty:
        st.info("No isolated fights detected with current settings.")
    else:
        # ── Header metrics ────────────────────────────────────────────────────
        total       = len(solo_kills)
        order_iso   = (solo_kills["isolated_team"] == "ORDER").sum()
        chaos_iso   = (solo_kills["isolated_team"] == "CHAOS").sum()
        type_counts = solo_kills["fight_type"].value_counts()

        c1, c2, c3 = st.columns(3)
        c1.metric("Total Isolated Fights", total)
        c2.metric("ORDER Players Isolated", int(order_iso))
        c3.metric("CHAOS Players Isolated", int(chaos_iso))

        # Fight type breakdown
        type_cols = st.columns(len(type_counts))
        for i, (ftype, count) in enumerate(type_counts.items()):
            type_cols[i].metric(ftype, count)

        st.markdown("---")

        # ── Timeline scatter ──────────────────────────────────────────────────
        st.markdown("<div class='section-header'>Isolated Fight Timeline</div>",
                    unsafe_allow_html=True)
        st.caption("Marker color = team of the ISOLATED player (the one who had no backup)")

        fig_solo = go.Figure()

        for team, color in TEAM_COLORS.items():
            t = solo_kills[solo_kills["isolated_team"] == team]
            if t.empty:
                continue
            fig_solo.add_trace(go.Scatter(
                x=t["minute"],
                y=t["fight_type"],
                mode="markers",
                marker=dict(size=14, color=color, symbol="x",
                            line=dict(width=2, color=color)),
                name=f"{team} isolated",
                hovertemplate=(
                    "<b>%{customdata[0]}</b> isolated<br>"
                    "Killed by: <b>%{customdata[1]}</b><br>"
                    "Fight: %{customdata[2]}<br>"
                    "Time: %{x:.1f} min<br>"
                    "<extra></extra>"
                ),
                customdata=t[["isolated_champ", "killer_champ", "fight_type"]].values,
            ))

        fig_solo.update_layout(
            height=250,
            xaxis_title="Game Time (min)",
            yaxis_title="Fight Type",
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            hovermode="closest",
        )
        st.plotly_chart(fig_solo, use_container_width=True)

        st.markdown("---")

        # ── Kill log ──────────────────────────────────────────────────────────
        st.markdown("<div class='section-header'>Fight Log</div>",
                    unsafe_allow_html=True)

        log = solo_kills.copy()
        log["Time"]      = log["minute"].round(2).astype(str) + " min"
        log["Type"]      = log["fight_type"]
        log["Isolated"]  = log["isolated_champ"] + " (" + log["isolated_team"] + ")"
        log["Killer"]    = log["killer_champ"] + " (" + log["killer_team"] + ")"
        log["Victim"]    = log["victim_champ"] + " (" + log["victim_team"] + ")"
        log["Assisters"] = log["assisters"]
        st.dataframe(
            log[["Time", "Type", "Isolated", "Killer", "Victim", "Assisters"]],
            hide_index=True,
            use_container_width=True,
        )

        st.markdown("---")

        # ── Per-player summary ────────────────────────────────────────────────
        st.markdown("<div class='section-header'>Per-Player Breakdown</div>",
                    unsafe_allow_html=True)
        st.caption("Sorted by isolated deaths — players who most often got caught alone")

        if not solo_summary.empty:
            col_o, col_c = st.columns(2)
            for col, team in [(col_o, "ORDER"), (col_c, "CHAOS")]:
                with col:
                    tag_cls = "tag-order" if team == "ORDER" else "tag-chaos"
                    st.markdown(
                        f"<span class='tag {tag_cls}'>{team}</span>",
                        unsafe_allow_html=True,
                    )
                    t_df = solo_summary[solo_summary["team"] == team][
                        ["player", "champion",
                         "isolated_deaths", "isolated_kills", "assisted_kills"]
                    ].rename(columns={
                        "player":           "Player",
                        "champion":         "Champion",
                        "isolated_deaths":  "Died Alone",
                        "isolated_kills":   "Killed Alone",
                        "assisted_kills":   "Killed w/ Backup",
                    })
                    st.dataframe(t_df, hide_index=True, use_container_width=True)

        st.caption(
            "**Died Alone** — player was the isolated side and died.  "
            "**Killed Alone** — player was the isolated side but got the kill.  "
            "**Killed w/ Backup** — player had teammates assisting the kill."
        )