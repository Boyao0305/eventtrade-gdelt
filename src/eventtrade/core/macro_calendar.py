"""Calendar of US scheduled macro announcements and NYSE session exceptions, 2015-01 to 2025-03.

Dates and times only, transcribed from official sources (no market data):

FOMC (Board of Governors of the Federal Reserve System)
- https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (2021-2025)
- https://www.federalreserve.gov/monetarypolicy/fomchistorical2015.htm ... fomchistorical2020.htm
- statement press releases, e.g.
  https://www.federalreserve.gov/newsevents/pressreleases/monetary20200303a.htm (10:00 a.m.),
  .../monetary20200315a.htm (5:00 p.m.), .../monetary20200323a.htm (8:00 a.m.),
  .../monetary20191011a.htm, .../monetary20201105a.htm, .../monetary20250319a.htm (2:00 p.m.)
- press-conference pages, e.g. .../monetarypolicy/fomcpresconf20150917.htm, fomcpresconf20170920.htm

BLS (CPI and Employment Situation)
- https://www.bls.gov/bls/news-release/cpi.htm (archived CPI releases, file names cpi_MMDDYYYY)
- https://www.bls.gov/bls/news-release/empsit.htm (archived Employment Situation releases)
- cross-check: https://www.bls.gov/schedule/<year>/home.htm for every year 2015-2025

NYSE (Intercontinental Exchange)
- https://www.nyse.com/markets/hours-calendars, as archived by the Wayback Machine on
  2016-02, 2017-06, 2018-12, 2019-02, 2022-02 and 2025-02 (each snapshot lists 2-4 years)
- https://ir.theice.com/press/news-details/2018/New-York-Stock-Exchange-to-Honor-President-George-H-W-Bush/default.aspx
- https://ir.theice.com/press/news-details/2024/The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of-Mourning/default.aspx

All times are US Eastern (ET). ISO date strings, sorted ascending.
"""

# FOMC statement release day = last day of each scheduled two-day meeting, as originally
# scheduled (8 per year). Every scheduled statement in this period was released at 14:00 ET
# (also the non-press-conference meetings, e.g. 2015-04-29; cf. the minutes' "to be released
# at 2:00 p.m."). 2020-03-18 was cancelled (see FOMC_CANCELLED) but stays here as scheduled.
FOMC_SCHEDULED: tuple[str, ...] = (
    # 2015
    "2015-01-28",
    "2015-03-18",
    "2015-04-29",
    "2015-06-17",
    "2015-07-29",
    "2015-09-17",
    "2015-10-28",
    "2015-12-16",
    # 2016
    "2016-01-27",
    "2016-03-16",
    "2016-04-27",
    "2016-06-15",
    "2016-07-27",
    "2016-09-21",
    "2016-11-02",
    "2016-12-14",
    # 2017
    "2017-02-01",
    "2017-03-15",
    "2017-05-03",
    "2017-06-14",
    "2017-07-26",
    "2017-09-20",
    "2017-11-01",
    "2017-12-13",
    # 2018
    "2018-01-31",
    "2018-03-21",
    "2018-05-02",
    "2018-06-13",
    "2018-08-01",
    "2018-09-26",
    "2018-11-08",
    "2018-12-19",
    # 2019
    "2019-01-30",
    "2019-03-20",
    "2019-05-01",
    "2019-06-19",
    "2019-07-31",
    "2019-09-18",
    "2019-10-30",
    "2019-12-11",
    # 2020 (Nov 4-5 meeting: statement on Thursday Nov 5, after the election)
    "2020-01-29",
    "2020-03-18",
    "2020-04-29",
    "2020-06-10",
    "2020-07-29",
    "2020-09-16",
    "2020-11-05",
    "2020-12-16",
    # 2021
    "2021-01-27",
    "2021-03-17",
    "2021-04-28",
    "2021-06-16",
    "2021-07-28",
    "2021-09-22",
    "2021-11-03",
    "2021-12-15",
    # 2022
    "2022-01-26",
    "2022-03-16",
    "2022-05-04",
    "2022-06-15",
    "2022-07-27",
    "2022-09-21",
    "2022-11-02",
    "2022-12-14",
    # 2023
    "2023-02-01",
    "2023-03-22",
    "2023-05-03",
    "2023-06-14",
    "2023-07-26",
    "2023-09-20",
    "2023-11-01",
    "2023-12-13",
    # 2024
    "2024-01-31",
    "2024-03-20",
    "2024-05-01",
    "2024-06-12",
    "2024-07-31",
    "2024-09-18",
    "2024-11-07",
    "2024-12-18",
    # 2025 (through March)
    "2025-01-29",
    "2025-03-19",
)

# Scheduled statement days on which no scheduled 14:00 statement was released.
FOMC_CANCELLED: tuple[str, ...] = (
    # 2020: March 17-18 meeting cancelled, superseded by the unscheduled 2020-03-15 action
    "2020-03-18",
)

# Scheduled statement days followed by a Chair press conference (14:30 ET):
# quarterly (Mar/Jun/Sep/Dec) through 2018, every meeting from January 2019.
# The unscheduled 2020-03-15 action also had a press call; it is not listed here.
FOMC_PRESS_CONFERENCE: tuple[str, ...] = (
    # 2015
    "2015-03-18",
    "2015-06-17",
    "2015-09-17",
    "2015-12-16",
    # 2016
    "2016-03-16",
    "2016-06-15",
    "2016-09-21",
    "2016-12-14",
    # 2017
    "2017-03-15",
    "2017-06-14",
    "2017-09-20",
    "2017-12-13",
    # 2018
    "2018-03-21",
    "2018-06-13",
    "2018-09-26",
    "2018-12-19",
    # 2019
    "2019-01-30",
    "2019-03-20",
    "2019-05-01",
    "2019-06-19",
    "2019-07-31",
    "2019-09-18",
    "2019-10-30",
    "2019-12-11",
    # 2020 (2020-03-18 cancelled)
    "2020-01-29",
    "2020-04-29",
    "2020-06-10",
    "2020-07-29",
    "2020-09-16",
    "2020-11-05",
    "2020-12-16",
    # 2021
    "2021-01-27",
    "2021-03-17",
    "2021-04-28",
    "2021-06-16",
    "2021-07-28",
    "2021-09-22",
    "2021-11-03",
    "2021-12-15",
    # 2022
    "2022-01-26",
    "2022-03-16",
    "2022-05-04",
    "2022-06-15",
    "2022-07-27",
    "2022-09-21",
    "2022-11-02",
    "2022-12-14",
    # 2023
    "2023-02-01",
    "2023-03-22",
    "2023-05-03",
    "2023-06-14",
    "2023-07-26",
    "2023-09-20",
    "2023-11-01",
    "2023-12-13",
    # 2024
    "2024-01-31",
    "2024-03-20",
    "2024-05-01",
    "2024-06-12",
    "2024-07-31",
    "2024-09-18",
    "2024-11-07",
    "2024-12-18",
    # 2025 (through March)
    "2025-01-29",
    "2025-03-19",
)

# Unscheduled FOMC policy announcements: (ISO date, "HH:MM" ET of the statement release).
# Deliberately excluded (not a change in the policy stance, or not an FOMC policy-rate /
# asset-purchase decision):
# - 2019-10-11 11:00 "Statement Regarding Monetary Policy Implementation" (T-bill purchases;
#   the statement says it does "not represent a change in the stance of monetary policy");
#   the 2019-10-04 unscheduled call itself produced no announcement.
# - 2020-03-19 and 2020-03-31 notation votes (central-bank swap lines, FIMA repo facility).
# - 2020-08-27 notation vote (Statement on Longer-Run Goals and Monetary Policy Strategy).
# - 2025-08-22 notation vote (out of range).
FOMC_UNSCHEDULED: tuple[tuple[str, str], ...] = (
    # 2020
    ("2020-03-03", "10:00"),  # -50 bp, unscheduled call of March 2
    ("2020-03-15", "17:00"),  # Sunday: to 0-1/4 %, asset purchases; replaced the Mar 17-18 meeting
    ("2020-03-23", "08:00"),  # purchases "in the amounts needed" (open-ended)
)

# CPI news releases, 08:30 ET. Dates taken from the BLS release archive (actual release
# dates) and matched one-for-one against the BLS annual schedules for 2015-2025. No release
# in this range was shifted: the Dec 2018 - Jan 2019 shutdown did not affect BLS (Department
# of Labor was funded), and the Oct 2013 / Oct-Nov 2025 shutdown delays are out of range.
# Release month = reference month + 1 (the January 2015 release is the December 2014 CPI).
# Two releases fell on Good Friday, an NYSE holiday: 2017-04-14 and 2020-04-10.
CPI_RELEASES: tuple[str, ...] = (
    # 2015
    "2015-01-16",
    "2015-02-26",
    "2015-03-24",
    "2015-04-17",
    "2015-05-22",
    "2015-06-18",
    "2015-07-17",
    "2015-08-19",
    "2015-09-16",
    "2015-10-15",
    "2015-11-17",
    "2015-12-15",
    # 2016
    "2016-01-20",
    "2016-02-19",
    "2016-03-16",
    "2016-04-14",
    "2016-05-17",
    "2016-06-16",
    "2016-07-15",
    "2016-08-16",
    "2016-09-16",
    "2016-10-18",
    "2016-11-17",
    "2016-12-15",
    # 2017
    "2017-01-18",
    "2017-02-15",
    "2017-03-15",
    "2017-04-14",
    "2017-05-12",
    "2017-06-14",
    "2017-07-14",
    "2017-08-11",
    "2017-09-14",
    "2017-10-13",
    "2017-11-15",
    "2017-12-13",
    # 2018
    "2018-01-12",
    "2018-02-14",
    "2018-03-13",
    "2018-04-11",
    "2018-05-10",
    "2018-06-12",
    "2018-07-12",
    "2018-08-10",
    "2018-09-13",
    "2018-10-11",
    "2018-11-14",
    "2018-12-12",
    # 2019 (Jan 11 released on schedule during the federal shutdown)
    "2019-01-11",
    "2019-02-13",
    "2019-03-12",
    "2019-04-10",
    "2019-05-10",
    "2019-06-12",
    "2019-07-11",
    "2019-08-13",
    "2019-09-12",
    "2019-10-10",
    "2019-11-13",
    "2019-12-11",
    # 2020
    "2020-01-14",
    "2020-02-13",
    "2020-03-11",
    "2020-04-10",
    "2020-05-12",
    "2020-06-10",
    "2020-07-14",
    "2020-08-12",
    "2020-09-11",
    "2020-10-13",
    "2020-11-12",
    "2020-12-10",
    # 2021
    "2021-01-13",
    "2021-02-10",
    "2021-03-10",
    "2021-04-13",
    "2021-05-12",
    "2021-06-10",
    "2021-07-13",
    "2021-08-11",
    "2021-09-14",
    "2021-10-13",
    "2021-11-10",
    "2021-12-10",
    # 2022
    "2022-01-12",
    "2022-02-10",
    "2022-03-10",
    "2022-04-12",
    "2022-05-11",
    "2022-06-10",
    "2022-07-13",
    "2022-08-10",
    "2022-09-13",
    "2022-10-13",
    "2022-11-10",
    "2022-12-13",
    # 2023
    "2023-01-12",
    "2023-02-14",
    "2023-03-14",
    "2023-04-12",
    "2023-05-10",
    "2023-06-13",
    "2023-07-12",
    "2023-08-10",
    "2023-09-13",
    "2023-10-12",
    "2023-11-14",
    "2023-12-12",
    # 2024
    "2024-01-11",
    "2024-02-13",
    "2024-03-12",
    "2024-04-10",
    "2024-05-15",
    "2024-06-12",
    "2024-07-11",
    "2024-08-14",
    "2024-09-11",
    "2024-10-10",
    "2024-11-13",
    "2024-12-11",
    # 2025 (through March)
    "2025-01-15",
    "2025-02-12",
    "2025-03-12",
)

# Employment Situation releases, 08:30 ET. Same sourcing and cross-check as CPI. Normally the
# first Friday; Thursday when that Friday is the Independence Day holiday (2015-07-02,
# 2020-07-02). 2025-01-10 was unaffected by the 2025-01-09 national day of mourning.
# Three releases fell on Good Friday, an NYSE holiday: 2015-04-03, 2021-04-02, 2023-04-07.
NFP_RELEASES: tuple[str, ...] = (
    # 2015
    "2015-01-09",
    "2015-02-06",
    "2015-03-06",
    "2015-04-03",
    "2015-05-08",
    "2015-06-05",
    "2015-07-02",
    "2015-08-07",
    "2015-09-04",
    "2015-10-02",
    "2015-11-06",
    "2015-12-04",
    # 2016
    "2016-01-08",
    "2016-02-05",
    "2016-03-04",
    "2016-04-01",
    "2016-05-06",
    "2016-06-03",
    "2016-07-08",
    "2016-08-05",
    "2016-09-02",
    "2016-10-07",
    "2016-11-04",
    "2016-12-02",
    # 2017
    "2017-01-06",
    "2017-02-03",
    "2017-03-10",
    "2017-04-07",
    "2017-05-05",
    "2017-06-02",
    "2017-07-07",
    "2017-08-04",
    "2017-09-01",
    "2017-10-06",
    "2017-11-03",
    "2017-12-08",
    # 2018
    "2018-01-05",
    "2018-02-02",
    "2018-03-09",
    "2018-04-06",
    "2018-05-04",
    "2018-06-01",
    "2018-07-06",
    "2018-08-03",
    "2018-09-07",
    "2018-10-05",
    "2018-11-02",
    "2018-12-07",
    # 2019
    "2019-01-04",
    "2019-02-01",
    "2019-03-08",
    "2019-04-05",
    "2019-05-03",
    "2019-06-07",
    "2019-07-05",
    "2019-08-02",
    "2019-09-06",
    "2019-10-04",
    "2019-11-01",
    "2019-12-06",
    # 2020
    "2020-01-10",
    "2020-02-07",
    "2020-03-06",
    "2020-04-03",
    "2020-05-08",
    "2020-06-05",
    "2020-07-02",
    "2020-08-07",
    "2020-09-04",
    "2020-10-02",
    "2020-11-06",
    "2020-12-04",
    # 2021
    "2021-01-08",
    "2021-02-05",
    "2021-03-05",
    "2021-04-02",
    "2021-05-07",
    "2021-06-04",
    "2021-07-02",
    "2021-08-06",
    "2021-09-03",
    "2021-10-08",
    "2021-11-05",
    "2021-12-03",
    # 2022
    "2022-01-07",
    "2022-02-04",
    "2022-03-04",
    "2022-04-01",
    "2022-05-06",
    "2022-06-03",
    "2022-07-08",
    "2022-08-05",
    "2022-09-02",
    "2022-10-07",
    "2022-11-04",
    "2022-12-02",
    # 2023
    "2023-01-06",
    "2023-02-03",
    "2023-03-10",
    "2023-04-07",
    "2023-05-05",
    "2023-06-02",
    "2023-07-07",
    "2023-08-04",
    "2023-09-01",
    "2023-10-06",
    "2023-11-03",
    "2023-12-08",
    # 2024
    "2024-01-05",
    "2024-02-02",
    "2024-03-08",
    "2024-04-05",
    "2024-05-03",
    "2024-06-07",
    "2024-07-05",
    "2024-08-02",
    "2024-09-06",
    "2024-10-04",
    "2024-11-01",
    "2024-12-06",
    # 2025 (through March)
    "2025-01-10",
    "2025-02-07",
    "2025-03-07",
)

# NYSE early closes (13:00 ET): day before Independence Day when a weekday trading day, day
# after Thanksgiving, Christmas Eve when a weekday trading day. None in 2025-01..03.
NYSE_EARLY_CLOSES: tuple[str, ...] = (
    # 2015 (July 3 was the observed holiday)
    "2015-11-27",
    "2015-12-24",
    # 2016 (Dec 24 a Saturday)
    "2016-11-25",
    # 2017 (Dec 24 a Sunday)
    "2017-07-03",
    "2017-11-24",
    # 2018
    "2018-07-03",
    "2018-11-23",
    "2018-12-24",
    # 2019
    "2019-07-03",
    "2019-11-29",
    "2019-12-24",
    # 2020 (July 3 was the observed holiday)
    "2020-11-27",
    "2020-12-24",
    # 2021 (Dec 24 was the observed Christmas holiday)
    "2021-11-26",
    # 2022 (Dec 24 a Saturday)
    "2022-11-25",
    # 2023 (Dec 24 a Sunday)
    "2023-07-03",
    "2023-11-24",
    # 2024
    "2024-07-03",
    "2024-11-29",
    "2024-12-24",
)

# NYSE full-day weekday closures (exchange holidays plus special closures).
# New Year's Day 2022 fell on a Saturday: no holiday observed (NYSE Rule 7.2).
NYSE_HOLIDAYS: tuple[str, ...] = (
    # 2015
    "2015-01-01",
    "2015-01-19",
    "2015-02-16",
    "2015-04-03",
    "2015-05-25",
    "2015-07-03",
    "2015-09-07",
    "2015-11-26",
    "2015-12-25",
    # 2016
    "2016-01-01",
    "2016-01-18",
    "2016-02-15",
    "2016-03-25",
    "2016-05-30",
    "2016-07-04",
    "2016-09-05",
    "2016-11-24",
    "2016-12-26",
    # 2017
    "2017-01-02",
    "2017-01-16",
    "2017-02-20",
    "2017-04-14",
    "2017-05-29",
    "2017-07-04",
    "2017-09-04",
    "2017-11-23",
    "2017-12-25",
    # 2018
    "2018-01-01",
    "2018-01-15",
    "2018-02-19",
    "2018-03-30",
    "2018-05-28",
    "2018-07-04",
    "2018-09-03",
    "2018-11-22",
    "2018-12-05",  # special closure: national day of mourning, President George H. W. Bush
    "2018-12-25",
    # 2019
    "2019-01-01",
    "2019-01-21",
    "2019-02-18",
    "2019-04-19",
    "2019-05-27",
    "2019-07-04",
    "2019-09-02",
    "2019-11-28",
    "2019-12-25",
    # 2020
    "2020-01-01",
    "2020-01-20",
    "2020-02-17",
    "2020-04-10",
    "2020-05-25",
    "2020-07-03",
    "2020-09-07",
    "2020-11-26",
    "2020-12-25",
    # 2021
    "2021-01-01",
    "2021-01-18",
    "2021-02-15",
    "2021-04-02",
    "2021-05-31",
    "2021-07-05",
    "2021-09-06",
    "2021-11-25",
    "2021-12-24",
    # 2022 (Juneteenth observed from 2022)
    "2022-01-17",
    "2022-02-21",
    "2022-04-15",
    "2022-05-30",
    "2022-06-20",
    "2022-07-04",
    "2022-09-05",
    "2022-11-24",
    "2022-12-26",
    # 2023
    "2023-01-02",
    "2023-01-16",
    "2023-02-20",
    "2023-04-07",
    "2023-05-29",
    "2023-06-19",
    "2023-07-04",
    "2023-09-04",
    "2023-11-23",
    "2023-12-25",
    # 2024
    "2024-01-01",
    "2024-01-15",
    "2024-02-19",
    "2024-03-29",
    "2024-05-27",
    "2024-06-19",
    "2024-07-04",
    "2024-09-02",
    "2024-11-28",
    "2024-12-25",
    # 2025 (through March)
    "2025-01-01",
    "2025-01-09",  # special closure: national day of mourning, President Jimmy Carter
    "2025-01-20",
    "2025-02-17",
)
