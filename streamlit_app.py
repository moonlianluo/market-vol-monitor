"""Public Streamlit entrypoint for the Market Vol Monitor."""

from datetime import date

import streamlit as st
import streamlit.components.v1 as components

from update_dashboard import make_payload, render_html


st.set_page_config(
    page_title="Market Vol Monitor",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data(ttl=3600, show_spinner=False)
def build_dashboard(as_of: str | None, refresh_version: int) -> str:
    """Fetch online data and build one dashboard per date for up to one hour."""
    del refresh_version  # changing it intentionally creates a fresh cache key
    payload = make_payload(offline=False, as_of=as_of)
    html = render_html(payload)
    # Streamlit owns these controls. The original controls require the local
    # /refresh and /asof HTTP endpoints, which do not exist in Community Cloud.
    control_css = """
    <style>
      #dateMode, #asofDate, #applyDateBtn, #refreshBtn { display:none !important; }
    </style>
    """
    return html.replace("</head>", control_css + "</head>", 1)


if "refresh_version" not in st.session_state:
    st.session_state.refresh_version = 0

with st.sidebar:
    st.title("Market Vol Monitor")
    st.caption(
        "A public research dashboard for realized volatility across "
        "U.S. Treasury rates and large-cap equities."
    )
    st.header("Data controls")
    mode = st.radio("As-of mode", ("Up to date", "Choose date"))
    chosen = None
    if mode == "Choose date":
        chosen_date = st.date_input(
            "As-of date",
            value=date.today(),
            min_value=date(1990, 1, 2),
            max_value=date.today(),
        )
        chosen = chosen_date.isoformat()

    if st.button("Refresh data", type="primary", use_container_width=True):
        st.cache_data.clear()
        st.session_state.refresh_version += 1
        st.rerun()

    st.caption(
        "Treasury yields come from Treasury.gov. Equity closes use yfinance "
        "with Stooq fallback. Data is cached for one hour."
    )
    with st.expander("Methodology"):
        st.markdown(
            "Realized volatility is the sample standard deviation of daily "
            "changes, annualized by √252. Rates are reported in basis points; "
            "equities use log returns and are reported in percent."
        )
    st.caption("Research use only — not investment advice.")

try:
    with st.spinner("Loading market data and calculating volatility..."):
        page = build_dashboard(chosen, st.session_state.refresh_version)
    components.html(page, height=6200, scrolling=True)
except Exception as exc:
    print(f"Dashboard build failed: {type(exc).__name__}: {exc}")
    st.error(
        "The dashboard could not be refreshed from its public data sources. "
        "Please try again later or run the local cached-data workflow."
    )

