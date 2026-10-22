"""fin — local personal finance dashboard.  Run:  uv run streamlit run app/streamlit_app.py"""

import streamlit as st
import ui
from views import cashflow, investments, overview, recurring, settings, spending, tags, transactions

st.set_page_config(page_title="fin", page_icon=":material/savings:", layout="wide")

PAGES = [
    st.Page(overview.render, title="Overview", icon=":material/dashboard:", url_path="overview", default=True),
    st.Page(cashflow.render, title="Cash flow", icon=":material/swap_vert:", url_path="cashflow"),
    st.Page(spending.render, title="Spending", icon=":material/pie_chart:", url_path="spending"),
    st.Page(recurring.render, title="Recurring", icon=":material/autorenew:", url_path="recurring"),
    st.Page(investments.render, title="Investments", icon=":material/trending_up:", url_path="investments"),
    st.Page(transactions.render, title="Transactions", icon=":material/receipt_long:", url_path="transactions"),
    st.Page(tags.render, title="Trips & tags", icon=":material/sell:", url_path="tags"),
    st.Page(settings.render, title="Settings & data", icon=":material/settings:", url_path="settings"),
]

nav = st.navigation(PAGES)
st.session_state["filters"] = ui.sidebar_filters()
ui.consent_banner()
nav.run()
