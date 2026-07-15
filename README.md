# Market Vol Monitor

Interactive rates and equity realized-volatility dashboard using Treasury.gov
par yields and equity daily closes from yfinance, with Stooq as a fallback.

## Run locally with Streamlit

```powershell
python -m pip install -r requirements.txt
streamlit run streamlit_app.py
```

The existing standalone local server remains available:

```powershell
python update_dashboard.py --serve
```

## Deploy

Push this repository to GitHub, then create an app in Streamlit Community Cloud
using `streamlit_app.py` as the entrypoint.
