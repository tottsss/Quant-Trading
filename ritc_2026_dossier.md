RITC 2026 Dossier (structured notes)

Sources
- Local PDF: RITC 2026 Case Package.pdf (this repo)
- Web: Rotman FRT-Lab RITC case files page, plus official RITC event page (see Web Findings section)
- Date compiled: 2026-02-01

Quick facts (combined)
- Event: Rotman International Trading Competition (RITC), hosted by Rotman School of Management, University of Toronto.
- Format: in-person, multi-case simulated trading competition using the Rotman Interactive Trader (RIT) platform.
- RITC 2026 dates: Feb 19-21, 2026 (3-day event).
- Location: Rotman School of Management, University of Toronto (St. George campus), Toronto, Canada.

Case overview (from local PDF)

0) Social Outcry (ice-breaker, not part of final score)
- Objective: open-outcry trading to kick off the event; build comfort with rapid decision-making.
- Format: 1 heat, 30 minutes.
- Instrument: RT100 futures (contract multiplier = 10).
- Market dynamics: price reacts to qualitative news; futures price is set by trades; spot follows a stochastic path with news shocks.
- Limits/fees: max 5 contracts per ticket; no commissions; no net position limits.
- Close-out: settles at closing spot price; P&L = sum over trades.

1) Liquidity Risk Case
- Objective: evaluate tender offers under liquidity risk; monetize tender vs market price differentials.
- Format: 2 heats; each heat has 5 sub-heats.
  - Sub-heat duration: 10 minutes (600 seconds)
  - Calendar time: 1 month (20 trading days)
- Instruments: multiple equities per sub-heat; parameters vary by sub-heat.
- Tender types: private tenders, competitive auctions, winner-take-all tenders.
- Execution: RIT API and RTD allowed.
- Limits/fees:
  - Gross limit 250,000 shares; net limit 150,000 shares
  - Max order size 10,000 shares
  - Commissions per sub-heat (see case package for each security)
  - No commission on tender offers
- Penalties (key):
  - Speculative / front-running shares: $1 per share up to 5,000 shares; $2 per share beyond 5,000
  - Uncovered shares at end of iteration: $10 per share
- Close-out: any open positions closed at end of each sub-heat at last traded price.

2) Volatility Trading Case
- Objective: trade volatility using options; build delta-hedged strategies.
- Format: 2 heats; each heat has 8 sub-heats.
  - Sub-heat duration: 5 minutes (300 seconds)
  - Calendar time: 1 month (20 trading days)
- Instruments: RTM ETF (non-dividend); 1-month European options.
  - Calls/puts with strikes 45-54 (10 strikes, 20 contracts total).
- Execution: RIT API and RTD allowed.
- Market model notes:
  - RTM follows random-walk; options quoted via Black-Scholes with zero variance risk premium.
  - Analysts provide realized vol updates; mispricing can occur after volatility shifts.
- Delta limits and penalties:
  - Delta limit announced via news at sub-heat start; penalty rate also announced.
  - Penalty applies per second when delta exceeds limit: (|delta| - delta_limit) * rate.
- Limits/fees:
  - RTM: gross/net 50,000 shares; max trade size 10,000 shares; fee $0.01 per share.
  - Options: gross 2,500 contracts; net 1,000 contracts; max trade size 100 contracts; fee $1.00 per contract.
- Close-out: RTM closed at last price; options cash-settled at expiration.

3) GBE Energy Electricity Trading Case
- Objective: role-based team trading across production, distribution, and trading functions in a regulated market.
- Format: 2 heats; each heat has 4 sub-heats.
  - Sub-heat duration: 15 minutes (900 seconds)
  - Calendar time: 5 trading days (first week of August)
- Roles:
  - Producer (1): manages solar + natural gas production, sells electricity.
  - Distributor (1): buys power, sells to customers at $70/MWh.
  - Trader (2): handles institutional orders, provides liquidity.
- Instruments:
  - ELEC-dayX (spot), 100 MWh per contract; tradeable on day X only.
  - ELEC-F (forward), 500 MWh per contract; delivers next day.
  - NG (natural gas spot), 100 MMBtu per contract; Producer only.
- Production details:
  - Solar output depends on sunshine hours; forecasts update 3x per day; final update is accurate.
  - Natural gas conversion: 800 MMBtu -> 100 MWh (8 NG contracts -> 1 ELEC-dayX contract).
  - Producers can use up to 10 gas plants; max 80 NG -> 10 ELEC-dayX.
- Demand model for Distributor customers:
  - ELEC_demand = 200 - 15*AT + 0.8*AT^2 - 0.01*AT^3
  - AT = expected average temperature (C); updates 3x per day; final update is accurate.
- Market rules:
  - Spot market prices and volumes set by regulator (RAE) with daily bulletins; limited volume.
  - Forward market is recommended to avoid spot volume constraints.
- Limits/fees:
  - Max trade size: ELEC-F 10 contracts; NG 80 contracts.
  - Max net positions: ELEC-dayX 300; NG 80 (Producer); ELEC-F 60.
  - No transaction costs for ELEC-F or NG; ELEC-F quotes are integer prices.
- Penalties / close-out:
  - Excess demand penalty for Distributors: $20,000 per contract short.
  - End-of-day open ELEC-dayX positions are closed at $0; long positions are fined $20,000 per contract.

4) Merger Arbitrage Case
- Objective: trade deal spreads across 5 announced M&A deals; hedge by deal structure.
- Format: 2 heats; 5 sub-heats; 10 minutes each (600 seconds), 6 months calendar time.
- Instruments: 10 stocks (5 targets, 5 acquirers); deal terms fixed during sub-heat.
- Deal set (targets/acquirers):
  - D1 TGX / PHR (all-cash $50)
  - D2 BYL / CLD (stock-for-stock 0.75 CLD per BYL)
  - D3 GGD / PNR (mixed $33 + 0.20 PNR)
  - D4 FSR / ATB (all-cash $40)
  - D5 SPK / EEC (stock-for-stock 1.20 EEC per SPK)
- News categories: REG, FIN, SHR, ALT, PRC; severity impacts probability updates.
- Anchors: initial completion probabilities provided at t=0 (per deal).
- Limits/fees:
  - Gross limit 100,000 shares; net limit 50,000 shares.
  - Max order size 5,000 shares; commission $0.02 per share.
- Close-out: positions closed at end of each sub-heat; deal outcomes can resolve mid-heat.

5) Algorithmic Market Making Case
- Objective: algorithmic market making across 4 stocks; manage inventory vs spread capture.
- Format: 12 heats; 5 minutes each; 1 team member trades all heats.
- Execution: algorithmic only; RIT API and RTD allowed; 2 minutes between heats to reload.
- Instruments: SPNG, SMMR, ATMN, WNTR (all start at $25; CAD).
  - Market order fee $0.02/share for all.
  - Passive rebates: SPNG $0.01; SMMR $0.02; ATMN $0.015; WNTR $0.025.
  - Max order size: 10,000 shares.
- Aggregate position limit:
  - Limit announced at start of each heat.
  - Penalty: $10 per share above limit, assessed each minute (market close).
- Close-out: all positions closed at last price at heat end.

Scoring methodology (from local PDF)
- Five scored cases (Liquidity Risk, Volatility, GBE Energy, Merger Arb, Algo MM) each weighted 20%.
- Team P&L is ranked within each sub-heat/heat; ranks are averaged to form case rank.
- Case rank maps to score (higher is better); weighted sum gives final score.
- Ties broken by variance of case scores; lower variance wins.
- Awards: top 5 teams receive CAD 5,000 / 2,500 / 1,500 / 1,000 / 500.

Web findings (official sources)
- Case files and downloads are published on the Rotman FRT-Lab RITC case files page, including the 2026 case package and official utilities.
- Official utilities listed there include the RIT User Application (Windows), RTD documentation, and installation instructions, plus a Mac VMware guide.
- The official RITC page lists the competition dates as Feb 19-21, 2026 (in-person format).

Web PDFs downloaded (official)
- ritc_2026_web_pdfs/RITC_2026_Case_Package_web.pdf
- ritc_2026_web_pdfs/RIT_2026_User_App_and_RTD_Installation_Instructions.pdf
- ritc_2026_web_pdfs/RIT_RTD_Documentation.pdf
- ritc_2026_web_pdfs/Installing_Windows_on_Mac_VMware_Fusion_RITC.pdf

Download sources (URLs)
- https://RotmanFRTL.github.io/RITC%202026%20Case%20Package.pdf
- https://306w.s3.amazonaws.com/rit_cases/Help/Student/RIT%20User%20App%20and%20RTD%20Installation%20Instructions.pdf
- https://306w.s3.amazonaws.com/rit_cases/Help/Student/RIT%20-%20User%20Guide%20-%20RTD%20Documentation.pdf
- https://RotmanFRTL.github.io//Installing%20Windows%20on%20a%20Mac%20using%20VMware%20Fusion%20-%20RITC.pdf
