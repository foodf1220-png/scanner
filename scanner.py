#!/usr/bin/env python3
"""
scanner.py - 미국 급등주 단타 실시간 스캐너 + 모의투자

[전략]
  검색   NYSE·NASDAQ·AMEX 보통주, 주가 $2 이상, 최근 2일 +20% 급등 없음, 오늘 +20% 이상
  신호   뉴욕 09:45~11:30 (한국 23:45~01:30, 서머타임 22:45~00:30) 에 마감한 1분봉이 모두 만족
           1) 전일 종가 대비 +20% 이상
           2) 종가 > VWAP
           3) 직전 1분봉 3개의 (최고가-최저가)/최저가 <= 3%
           4) 종가 < 직전 봉 고가
           5) 종가 >= 직전 봉 저가
         -> 다음 봉 시가 매수 (같은 종목은 하루 1번)
  청산   종목: +4% 익절 / -6% 손절
         계좌: 그날 계좌 손익(평가손익 포함)이 +5% 또는 -5% 면 전량 청산 후 그날 종료
         시간: 한국 03:00 (뉴욕 13:00, 서머타임 14:00) 남은 종목 전량 청산
  운용   계좌 5칸 (칸마다 계좌의 1/5), 칸이 비면 다음 신호에 다시 사용

[실행] 모든 실행은 결과와 상관없이 텔레그램 메시지가 최소 1개 감 (오류가 나면 오류 내용)
  python scanner.py                        전체 스캔: 실시간 전체 보통주(NASDAQ 공식 목록, 약 4천 개) 중
                                           가장 최근 장의 시그널 종목·날짜·종가 (없으면 '오늘은 시그널 없음')
  python scanner.py --list                 전체 스캔을 아래 TICKERS 고정 리스트로
  python scanner.py --codes AAPL TSLA      종목 조회: 최근 약 30일(yfinance 1분봉 한도) 시그널 날짜 목록
  python scanner.py --paper                실시간 모의투자 봇 (한국 21:00 이후 실행, 모의투자_시작.bat)
  python scanner.py --test-alert           알림 테스트

[텔레그램] 환경변수 TELEGRAM_TOKEN, TELEGRAM_CHAT_ID 에서 읽음 (코드에 직접 쓰지 않음)
           환경변수가 없을 때만 알림설정.json 의 telegram_token / telegram_chat_id 사용 (PC 로컬 실행용)
           API 호출은 파이썬 기본 모듈 urllib 사용 (requests 불필요)
[ntfy]     모의투자 봇 알림만. 알림설정.json 의 ntfy_topic 을 휴대폰 ntfy 앱에서 구독
[기록]     모의투자: logs/  bot_날짜.log, trades_날짜.csv, signals_날짜.csv, daily_summary.csv
[필요 패키지] yfinance, pandas, numpy  (GitHub Actions: pip install yfinance pandas)
[주의] 모의투자 전용 (실제 주문 없음). yfinance 분봉은 1~2분 늦을 수 있어 진입이 1~2분 늦어짐
"""
import argparse
import collections
import io
import json
import os
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, "logs")
ET, KST = ZoneInfo("America/New_York"), ZoneInfo("Asia/Seoul")

CFG = dict(
    kst_start="21:00",        # 운영 시작 (한국시간)
    kst_end="03:00",          # 강제 청산·종료 (한국시간, 다음날)
    entry_start_et="09:45",   # 신규 진입 시작 (뉴욕, 신호 봉 기준)
    entry_end_et="11:30",     # 신규 진입 종료 (뉴욕)
    min_price=2.0,            # 최소 주가 ($)
    surge=0.20,               # 오늘 상승률 기준 (+20%)
    box_bars=3,               # 직전 박스 봉 수 (1분봉)
    box_max=0.03,             # 박스 변동폭 상한 (3%)
    tp=4.0,                   # 종목 익절 %
    sl=6.0,                   # 종목 손절 %
    slots=5,                  # 계좌 분할 수 = 동시 보유 최대 종목 수
    day_target=5.0,           # 계좌 하루 +5% 면 전량 청산 후 종료
    day_stop=5.0,             # 계좌 하루 -5% 면 전량 청산 후 종료
    max_per_stock=1,          # 종목당 하루 최대 매매 횟수
    cost=0.5,                 # 왕복 비용 % (손익 계산에 반영)
    capital=10000.0,          # 모의투자 계좌 금액 ($)
    lag_min=1,                # 야후 분봉 지연 여유 (분)
)

# 고정 종목 리스트: python scanner.py --list 로 전체 스캔할 때 사용 (기본은 실시간 전체 목록)
TICKERS = [
    'A', 'AA', 'AAMI', 'AAOI', 'AAON', 'AAPL', 'ABBV', 'ABCB', 'ABG', 'ABNB', 'ABT', 'ACA', 'ACGL',
    'ACHC', 'ACIW', 'ACLS', 'ACM', 'ACMR', 'ADBE', 'ADC', 'ADI', 'ADM', 'ADP', 'ADSK', 'ADUS', 'AEE',
    'AEHR', 'AEIS', 'AEM', 'AEP', 'AFG', 'AFL', 'AFRM', 'AGCO', 'AGIO', 'AGL', 'AGM', 'AGO', 'AGX',
    'AGYS', 'AHR', 'AIG', 'AIN', 'AIR', 'AIT', 'AIZ', 'AJG', 'AKAM', 'ALAB', 'ALB', 'ALG', 'ALGM',
    'ALGN', 'ALGT', 'ALK', 'ALL', 'ALLY', 'ALNT', 'ALNY', 'ALRM', 'ALSN', 'ALV', 'AMAT', 'AMBQ', 'AMD',
    'AMG', 'AMGN', 'AMKR', 'AMP', 'AMR', 'AMSC', 'AMT', 'AMZN', 'AN', 'ANAB', 'ANDE', 'ANET', 'ANF',
    'AOS', 'APA', 'APAM', 'APD', 'APG', 'APGE', 'APH', 'APO', 'APP', 'APPF', 'AR', 'ARCB', 'ARE',
    'ARES', 'ARMK', 'AROC', 'ARW', 'ARWR', 'ARXS', 'ASB', 'ASH', 'ASO', 'ASTH', 'ASTS', 'ATEX', 'ATI',
    'ATKR', 'ATMU', 'ATO', 'ATR', 'ATRC', 'ATRO', 'AUB', 'AVA', 'AVAV', 'AVB', 'AVGO', 'AVNT', 'AVT',
    'AVY', 'AWI', 'AWK', 'AWR', 'AX', 'AXGN', 'AXON', 'AXP', 'AXS', 'AXSM', 'AXTI', 'AYI', 'AZO',
    'BA', 'BAC', 'BAH', 'BALL', 'BAND', 'BANR', 'BAP', 'BATRK', 'BBIO', 'BBY', 'BC', 'BCC', 'BCO',
    'BCPC', 'BDC', 'BDX', 'BE', 'BELFA', 'BELFB', 'BEN', 'BFAM', 'BFH', 'BH', 'BHE', 'BHF', 'BIIB',
    'BILL', 'BIO', 'BJ', 'BJRI', 'BKH', 'BKNG', 'BKR', 'BKU', 'BL', 'BLBD', 'BLDR', 'BLFS', 'BLK',
    'BLKB', 'BLLN', 'BMI', 'BMO', 'BMRN', 'BMY', 'BNY', 'BOH', 'BOKF', 'BOOT', 'BOX', 'BP', 'BPOP',
    'BR', 'BRKR', 'BRO', 'BROS', 'BSX', 'BSY', 'BTI', 'BTSG', 'BURL', 'BUSE', 'BWA', 'BWXT', 'BX',
    'BXP', 'BYD', 'BZH', 'C', 'CACC', 'CACI', 'CAH', 'CAKE', 'CALM', 'CALX', 'CAR', 'CARG', 'CARR',
    'CART', 'CASY', 'CAT', 'CATY', 'CAVA', 'CB', 'CBC', 'CBRE', 'CBRL', 'CBRS', 'CBSH', 'CBT', 'CBZ',
    'CCI', 'CCJ', 'CDNA', 'CDNL', 'CDNS', 'CDW', 'CE', 'CECO', 'CEG', 'CELC', 'CENX', 'CEVA', 'CF',
    'CFG', 'CFR', 'CG', 'CGNX', 'CHD', 'CHDN', 'CHEF', 'CHH', 'CHRD', 'CHRW', 'CHTR', 'CI', 'CIEN',
    'CINF', 'CL', 'CLH', 'CLMT', 'CLS', 'CLX', 'CM', 'CMC', 'CME', 'CMG', 'CMI', 'CMS', 'CNA',
    'CNC', 'CNI', 'CNK', 'CNM', 'CNMD', 'CNO', 'CNP', 'CNQ', 'CNR', 'CNX', 'COCO', 'COF', 'COGT',
    'COHR', 'COHU', 'COIN', 'COKE', 'COLB', 'COLM', 'CON', 'COO', 'COP', 'COR', 'CORT', 'COST', 'CPA',
    'CPAY', 'CPK', 'CPT', 'CR', 'CRAI', 'CRBG', 'CRC', 'CRCL', 'CRI', 'CRL', 'CRM', 'CRNX', 'CROX',
    'CRS', 'CRUS', 'CRWD', 'CRWV', 'CSCO', 'CSL', 'CSW', 'CSX', 'CTAS', 'CTRE', 'CTSH', 'CTVA', 'CUBI',
    'CURB', 'CVCO', 'CVI', 'CVLT', 'CVNA', 'CVS', 'CVX', 'CW', 'CWEN', 'CWST', 'CWT', 'CXT', 'CXW',
    'CYTK', 'D', 'DAL', 'DAR', 'DASH', 'DAVE', 'DB', 'DBX', 'DCI', 'DCO', 'DD', 'DDOG', 'DDS',
    'DE', 'DECK', 'DELL', 'DEO', 'DFNS', 'DG', 'DGII', 'DGX', 'DHI', 'DHR', 'DINO', 'DIOD', 'DIS',
    'DJCO', 'DK', 'DKS', 'DLB', 'DLR', 'DLTR', 'DNTH', 'DOCN', 'DOCU', 'DORM', 'DOV', 'DOW', 'DPZ',
    'DRI', 'DRS', 'DSGX', 'DT', 'DTE', 'DTM', 'DUK', 'DUOL', 'DVA', 'DVN', 'DXCM', 'DXPE', 'DY',
    'EAT', 'EBAY', 'ECG', 'ECHO', 'ECL', 'ECO', 'ECPG', 'ED', 'EEFT', 'EFX', 'EG', 'EGP', 'EHC',
    'EIX', 'EL', 'ELF', 'ELS', 'ELV', 'ELVN', 'EMBJ', 'EME', 'EMN', 'EMR', 'ENB', 'ENPH', 'ENS',
    'ENSG', 'ENTG', 'ENVA', 'EOG', 'EPAM', 'EPD', 'EPR', 'EPRT', 'EQH', 'EQIX', 'EQT', 'ERIE', 'ES',
    'ESAB', 'ESE', 'ESI', 'ESS', 'ETON', 'ETR', 'ETSY', 'EVR', 'EVRG', 'EW', 'EWBC', 'EWTX', 'EXC',
    'EXE', 'EXEL', 'EXLS', 'EXP', 'EXPD', 'EXPE', 'EXPO', 'EXR', 'FAF', 'FANG', 'FAST', 'FBIN', 'FBRX',
    'FCFS', 'FCN', 'FCNCA', 'FCX', 'FDS', 'FDX', 'FDXF', 'FE', 'FEIM', 'FELE', 'FERG', 'FFBC', 'FFIN',
    'FFIV', 'FHI', 'FIBK', 'FICO', 'FIS', 'FISV', 'FITB', 'FIVE', 'FIX', 'FLR', 'FLS', 'FMX', 'FND',
    'FNF', 'FORM', 'FOUR', 'FOX', 'FOXA', 'FPS', 'FR', 'FRPT', 'FRT', 'FSLR', 'FSS', 'FTAI', 'FTDR',
    'FTNT', 'FTV', 'FUL', 'FWONK', 'G', 'GATX', 'GBCI', 'GD', 'GDDY', 'GE', 'GEF', 'GEHC', 'GEV',
    'GFF', 'GGG', 'GH', 'GHC', 'GIB', 'GIL', 'GILD', 'GIS', 'GKOS', 'GL', 'GLPI', 'GLW', 'GM',
    'GMED', 'GNRC', 'GOLF', 'GOOGL', 'GPC', 'GPI', 'GPN', 'GRAL', 'GRMN', 'GS', 'GSAT', 'GSHD', 'GTLB',
    'GVA', 'GWRE', 'GWW', 'GXO', 'H', 'HAE', 'HAL', 'HALO', 'HAS', 'HASI', 'HCA', 'HCC', 'HCI',
    'HD', 'HEI', 'HGV', 'HHH', 'HIG', 'HII', 'HIW', 'HLI', 'HLIO', 'HLNE', 'HLT', 'HMC', 'HNGE',
    'HNI', 'HOMB', 'HON', 'HONA', 'HOOD', 'HP', 'HPE', 'HQY', 'HRB', 'HRI', 'HRMY', 'HROW', 'HSBC',
    'HSIC', 'HSY', 'HTO', 'HUBB', 'HUBG', 'HUBS', 'HUM', 'HURN', 'HUT', 'HWC', 'HWKN', 'HWM', 'HXL',
    'HZO', 'IBKR', 'IBM', 'IBOC', 'IBP', 'ICE', 'ICUI', 'IDA', 'IDCC', 'IDXX', 'IDYA', 'IESC', 'IEX',
    'IFF', 'ILMN', 'IMAX', 'IMO', 'IMVT', 'INCY', 'INDB', 'INDV', 'ING', 'INGR', 'INOD', 'INSM', 'INSP',
    'INSW', 'INTA', 'INTC', 'INTU', 'INVH', 'IOND', 'IONQ', 'IONS', 'IOT', 'IP', 'IPAR', 'IPGP', 'IQV',
    'IR', 'IRDM', 'IRM', 'IRON', 'IRTC', 'ISRG', 'IT', 'ITGR', 'ITRI', 'ITT', 'ITW', 'IVT', 'IVZ',
    'J', 'JAN', 'JAZZ', 'JBHT', 'JBL', 'JBTM', 'JEF', 'JJSF', 'JKHY', 'JLL', 'JNJ', 'JPM', 'JXN',
    'KAI', 'KALU', 'KBH', 'KBR', 'KDP', 'KEX', 'KEYS', 'KFY', 'KGS', 'KKR', 'KLAC', 'KLIC', 'KMB',
    'KMI', 'KMT', 'KN', 'KNF', 'KNSL', 'KNTK', 'KO', 'KOD', 'KR', 'KRC', 'KRMN', 'KRYS', 'KTB',
    'KTOS', 'KWR', 'KYMR', 'L', 'LAD', 'LAMR', 'LASR', 'LAUR', 'LAZ', 'LBRDK', 'LDOS', 'LEA', 'LEN',
    'LEU', 'LFUS', 'LGND', 'LH', 'LHX', 'LIF', 'LII', 'LIND', 'LINE', 'LITE', 'LLY', 'LLYVK', 'LMAT',
    'LMB', 'LMND', 'LMT', 'LNC', 'LNG', 'LNT', 'LNTH', 'LOAR', 'LOPE', 'LOW', 'LPG', 'LPLA', 'LPX',
    'LQDA', 'LRCX', 'LRN', 'LSCC', 'LSTR', 'LTH', 'LTM', 'LULU', 'LUV', 'LVS', 'LW', 'LXP', 'LYV',
    'MA', 'MAA', 'MAIN', 'MAIR', 'MAN', 'MANE', 'MANH', 'MAR', 'MAS', 'MATX', 'MBX', 'MC', 'MCD',
    'MCHP', 'MCK', 'MCO', 'MCY', 'MDB', 'MDGL', 'MDLN', 'MDLZ', 'MEDP', 'MELI', 'MEOH', 'MET', 'META',
    'MFC', 'MFP', 'MGA', 'MGM', 'MHK', 'MHO', 'MIAX', 'MIDD', 'MIRM', 'MKC', 'MKL', 'MKSI', 'MKTX',
    'MLI', 'MLM', 'MMM', 'MMS', 'MMSI', 'MNPR', 'MOD', 'MOH', 'MORN', 'MP', 'MPC', 'MPTI', 'MPWR',
    'MRCY', 'MRK', 'MRNA', 'MRSH', 'MRVL', 'MS', 'MSA', 'MSCI', 'MSFT', 'MSGE', 'MSGS', 'MSI', 'MSM',
    'MSTR', 'MTB', 'MTCH', 'MTD', 'MTDR', 'MTG', 'MTH', 'MTN', 'MTSI', 'MTZ', 'MU', 'MUR', 'MUSA',
    'MXL', 'MYRG', 'MZTI', 'NATL', 'NBIX', 'NDAQ', 'NDSN', 'NEE', 'NET', 'NEU', 'NFG', 'NFLX', 'NHC',
    'NHI', 'NI', 'NIC', 'NJR', 'NKE', 'NKTR', 'NNN', 'NOC', 'NOVT', 'NOW', 'NP', 'NPO', 'NRG',
    'NSC', 'NSIT', 'NSP', 'NTAP', 'NTCT', 'NTNX', 'NTRA', 'NTRS', 'NUE', 'NUTX', 'NVDA', 'NVEC', 'NVO',
    'NVR', 'NVS', 'NWE', 'NWPX', 'NWS', 'NXPI', 'NXST', 'NXT', 'NYT', 'O', 'OC', 'ODFL', 'OGE',
    'OGS', 'OHI', 'OII', 'OKE', 'OKTA', 'OLED', 'OLLI', 'OMC', 'OMCL', 'OMF', 'ON', 'ONTO', 'OPLN',
    'ORA', 'ORCL', 'ORI', 'ORKA', 'ORLY', 'OSIS', 'OSK', 'OTIS', 'OUST', 'OUT', 'OXY', 'OZK', 'PAAS',
    'PACS', 'PAG', 'PANW', 'PARR', 'PATK', 'PAY', 'PAYC', 'PAYX', 'PB', 'PBF', 'PBH', 'PCAR', 'PCOR',
    'PCTY', 'PCVX', 'PDFS', 'PECO', 'PEG', 'PEGA', 'PEN', 'PENG', 'PEP', 'PFG', 'PFGC', 'PFSI', 'PG',
    'PGR', 'PH', 'PHIN', 'PHM', 'PI', 'PII', 'PIPR', 'PJT', 'PKG', 'PLAB', 'PLD', 'PLNT', 'PLPC',
    'PLTR', 'PLUS', 'PLXS', 'PM', 'PNC', 'PNW', 'PODD', 'POOL', 'POR', 'POST', 'POWI', 'POWL', 'PPG',
    'PPL', 'PPLI', 'PRAX', 'PRDO', 'PRG', 'PRI', 'PRIM', 'PRK', 'PRKS', 'PRM', 'PRU', 'PSA', 'PSMT',
    'PSN', 'PSX', 'PTC', 'PTCT', 'PTGX', 'PVH', 'PVLA', 'PWR', 'PYPL', 'Q', 'QCOM', 'QLYS', 'QNT',
    'QRVO', 'QTWO', 'R', 'RAL', 'RAMP', 'RBA', 'RBC', 'RBLX', 'RBRK', 'RCI', 'RCL', 'RDDT', 'RDN',
    'RDNT', 'REG', 'REGN', 'REXR', 'RF', 'RGA', 'RGEN', 'RGLD', 'RH', 'RHI', 'RIO', 'RJF', 'RKLB',
    'RL', 'RLI', 'RMBS', 'RMD', 'RNG', 'RNR', 'RNST', 'ROAD', 'ROG', 'ROK', 'ROKU', 'ROL', 'ROP',
    'ROST', 'RPM', 'RRC', 'RRR', 'RRX', 'RS', 'RSG', 'RTX', 'RUSHA', 'RVMD', 'RVTY', 'RY', 'RYAN',
    'RYTM', 'SAFT', 'SAH', 'SAIA', 'SAIC', 'SAM', 'SANM', 'SBAC', 'SBCF', 'SBUX', 'SCCO', 'SCHW', 'SCI',
    'SEB', 'SEDG', 'SEI', 'SEIC', 'SEZL', 'SF', 'SFBS', 'SFM', 'SGI', 'SHAK', 'SHAZ', 'SHOO', 'SHW',
    'SIGI', 'SITE', 'SITM', 'SJM', 'SKM', 'SKT', 'SKWD', 'SKY', 'SKYW', 'SLAB', 'SLF', 'SLG', 'SLGN',
    'SM', 'SMA', 'SMCI', 'SMG', 'SMTC', 'SNA', 'SNDK', 'SNDR', 'SNEX', 'SNOW', 'SNPS', 'SNX', 'SO',
    'SOLS', 'SOLV', 'SON', 'SPB', 'SPCX', 'SPG', 'SPGI', 'SPHR', 'SPSC', 'SPXC', 'SQM', 'SR', 'SRE',
    'SRRK', 'SSB', 'SSD', 'SSNC', 'STAG', 'STEP', 'STM', 'STN', 'STOK', 'STRA', 'STRL', 'STT', 'STZ',
    'SU', 'SUI', 'SUNB', 'SUPN', 'SWK', 'SWKS', 'SWX', 'SXI', 'SXT', 'SYF', 'SYK', 'SYM', 'SYNA',
    'SYRE', 'SYY', 'TAP', 'TARS', 'TBBK', 'TCBI', 'TD', 'TDG', 'TDW', 'TDY', 'TEAM', 'TECH', 'TEM',
    'TENB', 'TER', 'TEX', 'TFC', 'TFX', 'TGT', 'TGTX', 'THC', 'THO', 'THRM', 'TIGO', 'TJX', 'TKO',
    'TKR', 'TLN', 'TM', 'TMDX', 'TMO', 'TMUS', 'TNET', 'TOL', 'TOST', 'TOWN', 'TPB', 'TPC', 'TPG',
    'TPL', 'TPR', 'TREX', 'TRGP', 'TRMB', 'TRNO', 'TROW', 'TRP', 'TRU', 'TRV', 'TSCO', 'TSLA', 'TSN',
    'TTAN', 'TTC', 'TTEK', 'TTMI', 'TTWO', 'TVTX', 'TW', 'TWLO', 'TWST', 'TXG', 'TXN', 'TXNM', 'TXRH',
    'TXT', 'TYL', 'U', 'UAL', 'UBER', 'UBSI', 'UCB', 'UCTT', 'UDR', 'UFPI', 'UFPT', 'UGI', 'UHS',
    'UI', 'ULS', 'ULTA', 'UMBF', 'UNF', 'UNFI', 'UNH', 'UNM', 'UNP', 'UPS', 'URBN', 'URI', 'USB',
    'USFD', 'UTHR', 'VAC', 'VC', 'VCEL', 'VCTR', 'VCYT', 'VECO', 'VEEV', 'VERA', 'VIAV', 'VICR', 'VIRT',
    'VKTX', 'VLO', 'VLTO', 'VMC', 'VMI', 'VNO', 'VNOM', 'VNT', 'VOYA', 'VOYG', 'VPG', 'VRNS', 'VRSK',
    'VRSN', 'VRT', 'VRTS', 'VRTX', 'VSAT', 'VSEC', 'VSH', 'VSNT', 'VST', 'VSXY', 'VTR', 'VVV', 'VVX',
    'VZ', 'W', 'WAB', 'WAFD', 'WAL', 'WAT', 'WBS', 'WCC', 'WDAY', 'WDC', 'WDFC', 'WEC', 'WELL',
    'WERN', 'WFC', 'WGS', 'WH', 'WHD', 'WHR', 'WING', 'WK', 'WKC', 'WLK', 'WLY', 'WM', 'WMB',
    'WMS', 'WMT', 'WRB', 'WRLD', 'WSBC', 'WSFS', 'WSM', 'WSO', 'WST', 'WTFC', 'WTM', 'WTRG', 'WTS',
    'WWD', 'XEL', 'XMTR', 'XOM', 'XPO', 'XYL', 'XYZ', 'YETI', 'YOU', 'YUMC', 'ZBH', 'ZBRA', 'ZD',
    'ZG', 'ZION', 'ZM', 'ZS', 'ZTS', 'ZWS'
]

EXCHANGES = ["NMS", "NGM", "NCM", "NYQ", "ASE"]   # 스크리너 거래소 코드: NASDAQ 3개 시장, NYSE, AMEX
NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
EXCLUDE_RE = r"\b(?:warrants?|rights?|units?|preferred|depositary|notes?|debentures?)\b|%"


# ═════════════════════ 휴대폰 알림 ═════════════════════
ALERT_FILE = os.path.join(HERE, "알림설정.json")


def load_alert():
    try:
        with open(ALERT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _http(url, data=None, headers=None, timeout=15):
    """파이썬 기본 모듈(urllib)로 요청. data 가 dict 면 폼 전송, bytes 면 그대로 전송"""
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def telegram_conf():
    """환경변수 TELEGRAM_TOKEN / TELEGRAM_CHAT_ID (없으면 알림설정.json - PC 로컬용)"""
    a = load_alert()
    return (os.environ.get("TELEGRAM_TOKEN") or a.get("telegram_token") or "",
            os.environ.get("TELEGRAM_CHAT_ID") or a.get("telegram_chat_id") or "")


def send_telegram(text):
    """텔레그램 전송. 4000자 넘으면 나눠서 보냄. 성공하면 True"""
    token, chat = telegram_conf()
    if not token or not chat:
        print("  ! 텔레그램 설정 없음: 환경변수 TELEGRAM_TOKEN, TELEGRAM_CHAT_ID 를 설정하세요")
        return False
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 4000:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    parts.append(cur)
    try:
        for p in parts:
            r = json.loads(_http(f"https://api.telegram.org/bot{token}/sendMessage",
                                 {"chat_id": chat, "text": p.strip(), "disable_web_page_preview": "true"}))
            if not r.get("ok"):
                print(f"  ! 텔레그램 응답 오류: {r}")
                return False
        return True
    except Exception as e:
        print(f"  ! 텔레그램 전송 실패: {e}")
        return False


def notify(text, title="급등주 단타"):
    """모의투자 봇 알림: ntfy + 텔레그램 (실패해도 매매는 계속)"""
    a, sent = load_alert(), []
    if a.get("ntfy_topic"):
        try:
            _http("https://ntfy.sh", json.dumps({"topic": a["ntfy_topic"], "title": title, "message": text}).encode("utf-8"),
                  {"Content-Type": "application/json"}, timeout=10)
            sent.append("ntfy")
        except Exception as e:
            print(f"  ! ntfy 알림 실패: {e}")
    if all(telegram_conf()) and send_telegram(f"[{title}] {text}"):
        sent.append("텔레그램")
    return sent


def test_alert():
    a = load_alert()
    token, chat = telegram_conf()
    if token and not chat:   # 텔레그램 채팅방 번호 자동 찾기 (봇에게 먼저 메시지를 보내야 함)
        try:
            r = json.loads(_http(f"https://api.telegram.org/bot{token}/getUpdates"))
            chats = [u["message"]["chat"]["id"] for u in r.get("result", []) if "message" in u]
            if chats:
                a["telegram_chat_id"] = str(chats[-1])
                with open(ALERT_FILE, "w", encoding="utf-8") as f:
                    json.dump(a, f, ensure_ascii=False, indent=2)
                print(f"텔레그램 Chat ID: {a['telegram_chat_id']} (알림설정.json 저장, 환경변수 TELEGRAM_CHAT_ID 로도 쓰세요)")
            else:
                print("텔레그램: 먼저 휴대폰에서 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요.")
        except Exception as e:
            print(f"텔레그램 확인 실패: {e}")
    sent = notify("알림 테스트입니다. 이 메시지가 보이면 휴대폰 연동 완료!")
    print(f"보낸 곳: {', '.join(sent) if sent else '없음 (환경변수 또는 알림설정.json 확인)'}")
    if a.get("ntfy_topic"):
        print(f"ntfy 앱에서 구독할 이름(topic): {a['ntfy_topic']}")


# ═════════════════════ 보통주 목록 ═════════════════════
def _read_pipe(url):
    df = pd.read_csv(io.StringIO(_http(url, timeout=30)), sep="|", dtype=str)
    return df[~df.iloc[:, 0].str.startswith("File Creation Time", na=False)]


def clean_name(name):
    """'Apple Inc. - Common Stock' -> 'Apple Inc.'"""
    return str(name).split(" - ")[0].replace(" Common Stock", "").strip()[:40]


def load_universe():
    """NYSE·NASDAQ·AMEX 보통주 -> {yfinance 표기 종목 코드: 회사명}"""
    nq = _read_pipe(NASDAQ_URL)
    nq = nq[(nq["Test Issue"] == "N") & (nq["ETF"] == "N")][["Symbol", "Security Name"]]
    ot = _read_pipe(OTHER_URL)
    ot = ot[(ot["Test Issue"] == "N") & (ot["ETF"] == "N") & ot["Exchange"].isin(["N", "A"])]
    ot = ot[["ACT Symbol", "Security Name"]].rename(columns={"ACT Symbol": "Symbol"})
    u = pd.concat([nq, ot], ignore_index=True).dropna()
    u = u[u["Security Name"].str.contains("common stock", case=False)]
    u = u[~u["Security Name"].str.contains(EXCLUDE_RE, case=False, regex=True)]
    u = u[~u["Symbol"].str.contains(r"[\$\^=]", regex=True)]
    return {s.replace(".", "-"): clean_name(n) for s, n in zip(u["Symbol"], u["Security Name"])}


# ═════════════════════ 시간 ═════════════════════
def _at(tz, hhmm, day):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


def session_bounds(d, cfg):
    """뉴욕 날짜 d 의 (진입 시작, 진입 종료, 강제 청산) - 09:30(뉴욕) 기준 분"""
    o = _at(ET, "09:30", d)

    def tod(dt):
        return (dt.astimezone(ET) - o).total_seconds() / 60

    ks, ke = _at(KST, cfg["kst_start"], d), _at(KST, cfg["kst_end"], d)
    if ke <= ks:
        ke += timedelta(days=1)
    t0 = max(tod(_at(ET, cfg["entry_start_et"], d)), tod(ks), 0.0)
    t1 = min(tod(_at(ET, cfg["entry_end_et"], d)), tod(ke))
    return t0, t1, min(tod(ke), 390.0)


def clock(d, tod):
    et = _at(ET, "09:30", d) + timedelta(minutes=float(tod))
    return f"{et:%H:%M}ET({et.astimezone(KST):%H:%M}KST)"


def tod_index(idx):
    return np.asarray(idx.hour * 60 + idx.minute - 570)


def prev_info(closes):
    """오늘 이전 종가들 -> (전일 종가, 최근 2일 +20% 급등 없음 여부)"""
    c = [float(x) for x in closes if x == x]
    if not c:
        return None
    chg = [c[i] / c[i - 1] - 1 for i in range(max(1, len(c) - 2), len(c))]
    return c[-1], all(x < 0.20 for x in chg)


# ═════════════════════ 매매 엔진 ═════════════════════
class TS:
    __slots__ = ("pc", "fresh", "cpv", "cv", "dvol", "box", "last", "t_last")

    def __init__(self, pc, fresh, nbox):
        self.pc, self.fresh = pc, fresh
        self.cpv = self.cv = self.dvol = 0.0
        self.box = collections.deque(maxlen=nbox)   # 직전 봉들 (고가, 저가)
        self.last, self.t_last = None, -1


class Engine:
    def __init__(self, cfg, d, log, alert=None):
        self.c, self.d, self.log = cfg, d, log
        self.alert = alert or (lambda msg: None)
        self.t0, self.t1, self.cut = session_bounds(d, cfg)
        self.st, self.pending, self.pos = {}, {}, {}
        self.cnt, self.last_exit, self.skipped = collections.Counter(), {}, 0
        self.realized, self.stopped = 0.0, None
        self.trades, self.signals = [], []

    def add(self, tk, pc, fresh):
        if tk not in self.st:
            self.st[tk] = TS(pc, fresh, self.c["box_bars"])

    def _bar(self, tk, t, o, h, l, c, v, allow_signal):
        """봉 하나 반영: VWAP·박스 갱신 + 신호 판정"""
        s = self.st[tk]
        s.cpv += (h + l + c) / 3 * v
        s.cv += v
        s.dvol += c * v
        chg = c / s.pc - 1
        sig = False
        if allow_signal and s.fresh and s.cv > 0 and len(s.box) == self.c["box_bars"]:
            bh = max(b[0] for b in s.box)
            bl = min(b[1] for b in s.box)
            ph, pl = s.box[-1]
            sig = (chg >= self.c["surge"] and c >= self.c["min_price"] and c > s.cpv / s.cv
                   and bl > 0 and (bh - bl) / bl <= self.c["box_max"] and pl <= c < ph)
        s.box.append((h, l))
        s.last, s.t_last = c, t
        if tk in self.pos:
            self.pos[tk]["last"] = c
        return sig, chg

    def on_minute(self, t, rows):
        """t: 09:30 기준 분, rows: {종목: (시, 고, 저, 종, 거래량)} - 그 분에 마감된 봉들"""
        if self.stopped:
            return
        for tk in [k for k in self.pos if k in rows]:
            self._exit_check(tk, t, rows[tk])
        for tk, r in rows.items():
            if tk not in self.st:
                continue
            sig, chg = self._bar(tk, t, *r, True)
            if not sig or not (self.t0 <= t < self.t1) or not self._may_signal(tk, t):
                continue
            self.cnt[tk] += 1
            self.pending[tk] = (t, self.st[tk].dvol)
            self.signals.append(dict(date=self.d, ticker=tk, time=clock(self.d, t), close=round(r[3], 4),
                                     chg=round(chg * 100, 1)))
            self.log(f"신호 {tk} {clock(self.d, t)} 종가 ${r[3]:.2f} ({chg * 100:+.1f}%) -> 매수 대기")

    def _may_signal(self, tk, t):
        return (tk not in self.pos and tk not in self.pending
                and self.cnt[tk] < self.c["max_per_stock"] and t > self.last_exit.get(tk, -1e9))

    def silent(self, tk, t, r):
        """늦게 들어온 지난 봉: 지표만 갱신 (보유 중이면 손절·익절 확인)"""
        if tk in self.pos:
            self._exit_check(tk, t, r)
        self._bar(tk, t, *r, False)

    def fill_pending(self, prices, t):
        """대기 중인 매수를 지금 가격으로 체결 (그날 거래대금 큰 종목 먼저)"""
        for tk in sorted(list(self.pending), key=lambda k: -self.pending[k][1]):
            if prices.get(tk):
                self._fill(tk, prices[tk], t)

    def _fill(self, tk, price, t):
        sig_t, _ = self.pending.pop(tk)
        if self.stopped or t + 1 > self.cut:
            return
        if len(self.pos) >= self.c["slots"]:
            self.skipped += 1
            self.log(f"매수 건너뜀 {tk}: 계좌 {self.c['slots']}칸 모두 사용 중")
            return
        e = float(price)
        self.pos[tk] = dict(entry=e, stop=e * (1 - self.c["sl"] / 100), tp=e * (1 + self.c["tp"] / 100),
                            tod=t, sig=sig_t, last=e)
        qty = int(self.c["capital"] / self.c["slots"] // e)
        p = self.pos[tk]
        self.log(f"매수 {tk} {clock(self.d, t)} ${e:.2f} x {qty}주 | 익절 ${p['tp']:.2f} "
                 f"손절 ${p['stop']:.2f} | 보유 {len(self.pos)}/{self.c['slots']}칸")
        self.alert(f"🟢 매수 {tk} ${e:.2f} ({(e / self.st[tk].pc - 1) * 100:+.1f}%) {clock(self.d, t)}\n"
                   f"익절 ${p['tp']:.2f} / 손절 ${p['stop']:.2f} | {qty}주 | 보유 {len(self.pos)}/{self.c['slots']}칸")

    def _exit_check(self, tk, t, r):
        p = self.pos[tk]
        if t < p["tod"]:
            return
        o, h, l = r[0], r[1], r[2]
        if l <= p["stop"]:     # 같은 봉에서 익절·손절 둘 다 닿으면 손절 우선, 갭 하락이면 시가
            self._close(tk, p["stop"] if t == p["tod"] else min(o, p["stop"]), t, "손절")
        elif h >= p["tp"]:
            self._close(tk, p["tp"] if t == p["tod"] else max(o, p["tp"]), t, "익절")

    def _close(self, tk, px, t, why):
        p = self.pos.pop(tk)
        self.last_exit[tk] = t
        gross = (px / p["entry"] - 1) * 100
        net = gross - self.c["cost"]
        self.realized += net / self.c["slots"]
        usd = self.c["capital"] / self.c["slots"] * net / 100
        self.trades.append(dict(date=self.d, ticker=tk, signal=clock(self.d, p["sig"]), entry_time=clock(self.d, p["tod"]),
                                exit_time=clock(self.d, t), entry=round(p["entry"], 4), exit=round(px, 4),
                                gross_pct=round(gross, 3), net_pct=round(net, 3), usd=round(usd, 2), why=why))
        self.log(f"매도 {tk} {clock(self.d, t)} ${px:.2f} {why} {net:+.2f}% (${usd:+.0f}) | 하루 손익 {self.realized:+.2f}%")
        self.alert(f"{'✅' if net > 0 else '🔻'} 매도 {tk} ${px:.2f} {why} {net:+.2f}% (${usd:+.0f})\n"
                   f"{clock(self.d, t)} | 오늘 계좌 {self.realized:+.2f}%")

    def total_pct(self, prices):
        """계좌 하루 손익 % = 실현 + 평가 (비용 반영)"""
        unreal = sum(((prices.get(tk) or p["last"]) / p["entry"] - 1) * 100 - self.c["cost"]
                     for tk, p in self.pos.items()) / self.c["slots"]
        return self.realized + unreal

    def risk(self, t, prices):
        """계좌 +목표 / -한도, 강제 청산 시각 점검"""
        if self.stopped:
            return
        total = self.total_pct(prices)
        if total <= -self.c["day_stop"]:
            self.liquidate(prices, t, "계좌 손실한도")
        elif total >= self.c["day_target"]:
            self.liquidate(prices, t, "계좌 목표달성")
        elif t + 1 >= self.cut:
            self.liquidate(prices, t, "03시 청산")

    def liquidate(self, prices, t, why):
        for tk in list(self.pos):
            self._close(tk, prices.get(tk) or self.pos[tk]["last"], t, why)
        self.pending.clear()
        self.stopped = why
        self.log(f"=== {why}: 전량 정리, 오늘 매매 종료 | 하루 손익 {self.realized:+.2f}% ===")
        if why.startswith("계좌"):
            self.alert(f"⏹ {why}: 전량 정리하고 오늘 매매 종료 | 오늘 계좌 {self.realized:+.2f}%")


# ═════════════════════ 데이터 (yfinance) ═════════════════════
def _split(df, tickers):
    out = {}
    if df is None or df.empty:
        return out
    if not isinstance(df.columns, pd.MultiIndex):
        df = pd.concat({tickers[0]: df}, axis=1)
    lv = set(df.columns.get_level_values(0))
    for t in tickers:
        if t in lv:
            sub = df[t].dropna(subset=["Close"])
            if not sub.empty:
                out[t] = sub
    return out


class LiveFeed:
    def __init__(self):
        import yfinance as yf
        self.yf = yf

    def now(self):
        return datetime.now(ET)

    def sleep_until(self, dt):
        while True:
            rem = (dt - datetime.now(ET)).total_seconds()
            if rem <= 0:
                return
            time.sleep(min(rem, 30))

    def universe(self):
        return load_universe()

    def screen(self):
        """지금 +20% 이상 오른 종목 (NYSE·NASDAQ·AMEX) -> {종목: (현재가, 상승률%)}"""
        yf = self.yf
        q = yf.EquityQuery("and", [yf.EquityQuery("gte", ["percentchange", CFG["surge"] * 100]),
                                   yf.EquityQuery("is-in", ["exchange", *EXCHANGES])])
        try:
            r = yf.screen(q, sortField="percentchange", sortAsc=False, size=250)
        except Exception:
            return None
        return {x["symbol"]: (x.get("regularMarketPrice"), x.get("regularMarketChangePercent"))
                for x in r.get("quotes", []) if x.get("symbol")}

    def daily(self, tickers, d):
        df = self.yf.download(tickers, period="1mo", interval="1d", auto_adjust=True,
                              group_by="ticker", progress=False, threads=True)
        got = _split(df, tickers)
        return {t: (prev_info(got[t]["Close"][[x.date() < d for x in got[t].index]].tolist()) if t in got else None)
                for t in tickers}

    def bars(self, tickers, d):
        if not tickers:
            return {}
        df = self.yf.download(tickers, period="1d", interval="1m", prepost=False, auto_adjust=True,
                              group_by="ticker", progress=False, threads=True)
        return {t: x[x.index.date == d] for t, x in _split(df, tickers).items() if (x.index.date == d).any()}

    def market_open(self, d):
        return bool(self.bars(["SPY"], d))


# ═════════════════════ 실시간 세션 ═════════════════════
def run_session(feed, cfg, tag="", live=True):
    d = feed.now().date()
    os.makedirs(LOGS, exist_ok=True)
    name = d.strftime("%Y%m%d") + tag
    logf = open(os.path.join(LOGS, f"bot_{name}.log"), "a", encoding="utf-8")

    def log(msg):
        n = feed.now()
        line = f"[{n:%H:%M:%S}ET {n.astimezone(KST):%H:%M}KST] {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    t0, t1, cut = session_bounds(d, cfg)
    o = _at(ET, "09:30", d)
    if d.weekday() >= 5:
        log("주말이라 장이 열리지 않습니다.")
        return
    if feed.now() >= o + timedelta(minutes=cut):
        log("오늘 운영 시간(한국 %s 까지)이 지났습니다." % cfg["kst_end"])
        return
    log(f"설정: 진입 {clock(d, t0)}~{clock(d, t1)}, 종목 +{cfg['tp']}% / -{cfg['sl']}%, "
        f"계좌 {cfg['slots']}칸, 계좌 +{cfg['day_target']}% / -{cfg['day_stop']}% 청산, 강제 청산 {clock(d, cut)}")
    universe = feed.universe()
    log(f"보통주 {len(universe)}개")
    alert = notify if live else None
    eng = Engine(cfg, d, log, alert)
    if live:
        sent = notify(f"📡 모의투자 시작 | 진입 한국 {clock(d, t0)[-9:-4]}~{clock(d, t1)[-9:-4]}, "
                      f"종목 +{cfg['tp']}/-{cfg['sl']}%, 계좌 ±{cfg['day_target']}%")
        log(f"휴대폰 알림: {', '.join(sent) if sent else '설정 없음 (알림설정.json)'}")
    known, tracked, prices, floor = {}, set(), {}, 0
    last_t, first = -1, True

    if feed.now() < o + timedelta(minutes=1):
        log(f"장 시작 대기 중 (뉴욕 09:30 = 한국 {o.astimezone(KST):%H:%M})")
        feed.sleep_until(o + timedelta(minutes=1, seconds=8))
    if not feed.market_open(d):
        log("오늘은 휴장일입니다.")
        return

    while True:
        now = feed.now()
        tod = (now - o).total_seconds() / 60
        floor = int(tod)
        if tod >= cut:
            eng.liquidate(prices, floor, "03시 청산")
            break

        # 1) 급등주 검색: 지금 +20% 이상 -> 보통주, 최근 2일 급등 없음, $2 근처 이상이면 감시
        hits = feed.screen()
        if hits is None:
            log("검색 응답 실패 - 다음 분에 다시 시도")
            hits = {}
        new = [s for s in hits if s in universe and s not in known]
        if new:
            for s, k in feed.daily(new, d).items():
                px, pct = hits[s]
                if k and px and pct is not None and abs((px / k[0] - 1) * 100 - pct) > 5:
                    log(f"제외 {s}: 가격 데이터 불일치 (분할 등 의심)")
                    k = None
                known[s] = k
        for s, (px, pct) in hits.items():
            k = known.get(s)
            if k and k[1] and s not in tracked and (px or 0) >= cfg["min_price"] * 0.9:
                tracked.add(s)
                eng.add(s, *k)
                log(f"감시 추가 {s} ${px:.2f} ({pct:+.1f}%)")

        # 2) 1분봉 + 현재가
        T = floor - 1 - cfg["lag_min"]
        want = sorted(tracked | set(eng.pos) | set(eng.pending))
        bars = feed.bars(want, d) if want else {}
        prices = {}
        for s, df in bars.items():
            tx = tod_index(df.index)
            cur, done = df[tx == floor], df[tx < floor]
            if len(cur):
                prices[s] = float(cur["Open"].iloc[0])
            elif len(done):
                prices[s] = float(done["Close"].iloc[-1])
        if first:
            last_t, first = T - 1, False

        # 3) 마감된 봉 처리: 지난 분은 지표만, 새 분은 신호·손익절까지
        by_min = collections.defaultdict(dict)
        for s, df in bars.items():
            if s not in eng.st:
                continue
            for tt, row in zip(tod_index(df.index), df[["Open", "High", "Low", "Close", "Volume"]].itertuples(index=False)):
                tt = int(tt)
                if tt <= eng.st[s].t_last or tt > T:
                    continue
                r = tuple(float(x) for x in row)
                if tt <= last_t:
                    eng.silent(s, tt, r)
                else:
                    by_min[tt][s] = r
        for tt in sorted(by_min):
            eng.on_minute(tt, by_min[tt])
        last_t = max(last_t, T)

        # 4) 매수 체결 + 계좌 ±기준·03시 점검
        eng.fill_pending(prices, floor)
        eng.risk(floor, prices)
        if eng.stopped:
            break
        if tod > t1 + 2 and not eng.pos and not eng.pending:
            log("진입 시간이 끝났고 보유 종목이 없어 오늘 매매를 마칩니다.")
            break
        if floor % 5 == 0:
            held = ", ".join(f"{s} {(prices.get(s, p['last']) / p['entry'] - 1) * 100:+.1f}%" for s, p in eng.pos.items())
            log(f"상태: 감시 {len(tracked)}종목, 보유 {len(eng.pos)}/{cfg['slots']}칸 [{held or '없음'}], "
                f"계좌 손익 {eng.total_pct(prices):+.2f}% (실현 {eng.realized:+.2f}%)")
        feed.sleep_until(o + timedelta(minutes=floor + 1, seconds=8))

    if eng.pos:
        eng.liquidate(prices, floor, "종료")
    pd.DataFrame(eng.trades).to_csv(os.path.join(LOGS, f"trades_{name}.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(eng.signals).to_csv(os.path.join(LOGS, f"signals_{name}.csv"), index=False, encoding="utf-8-sig")
    wins = sum(t["net_pct"] > 0 for t in eng.trades)
    if live:
        sp = os.path.join(LOGS, "daily_summary.csv")
        pd.DataFrame([dict(date=d, trades=len(eng.trades), wins=wins, day_pct=round(eng.realized, 3),
                           day_usd=round(eng.realized * cfg["capital"] / 100, 2), end=eng.stopped or "종료")]
                     ).to_csv(sp, mode="a", header=not os.path.exists(sp), index=False, encoding="utf-8-sig")
    log(f"오늘 결과: 매매 {len(eng.trades)}회 (수익 {wins}회), 하루 손익 {eng.realized:+.2f}% "
        f"(${eng.realized * cfg['capital'] / 100:+,.0f}) | 기록: {LOGS}")
    if live:
        notify(f"📊 오늘 결과: 매매 {len(eng.trades)}회 (수익 {wins}회)\n"
               f"계좌 {eng.realized:+.2f}% (${eng.realized * cfg['capital'] / 100:+,.0f}) | {eng.stopped or '종료'}")
    logf.close()
    return eng


# ═════════════════════ 한 번 실행 스캔 (전체 / --codes) ═════════════════════
def _yf():
    import yfinance as yf
    return yf


def download_batch(tickers, chunk=100, **kw):
    """여러 종목을 나눠 받아 {종목: DataFrame}"""
    yf, out = _yf(), {}
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        try:
            df = yf.download(part, group_by="ticker", auto_adjust=True, threads=True, progress=False, **kw)
            out.update(_split(df, part))
        except Exception as e:
            print(f"  ! 다운로드 실패 ({part[0]}~): {e}")
        print(f"  {min(i + chunk, len(tickers))}/{len(tickers)}", end="\r", flush=True)
    print()
    return out


def find_signals(code, d, m, pc, fresh, cfg=CFG):
    """하루치 1분봉 m 에서 시그널 찾기 (모의투자와 같은 엔진, 그날 첫 시그널) -> [dict(time, close, chg)]"""
    eng = Engine(cfg, d, log=lambda msg: None)
    eng.add(code, pc, fresh)
    for t, row in zip(tod_index(m.index), m[["Open", "High", "Low", "Close", "Volume"]].itertuples(index=False)):
        eng.on_minute(int(t), {code: tuple(float(x) for x in row)})
    return eng.signals


def data_ok(m, close_d):
    """일봉 종가와 1분봉 마지막 종가가 10% 넘게 다르면 분할 반영 오류 -> 제외"""
    return close_d > 0 and 0.9 <= float(m["Close"].iloc[-1]) / close_d <= 1.1


def scan_today(use_list=False):
    """전체 스캔: 가장 최근 장에서 시그널이 난 종목 -> 텔레그램 메시지 문자열
    use_list=False: 실시간 전체 보통주 목록(NASDAQ 공식) / True: TICKERS 고정 리스트"""
    yf = _yf()
    names = {} if use_list else load_universe()                    # {종목: 회사명}
    tickers = list(TICKERS) if use_list else sorted(names)
    src = f"TICKERS {len(tickers)}개" if use_list else f"실시간 전체 보통주 {len(tickers)}개"
    spy = yf.Ticker("SPY").history(period="5d", interval="1d")
    d = spy.index[-1].date()                                       # 가장 최근 장 날짜 (뉴욕)
    print(f"[1/3] {d} 일봉: {src}")
    daily = download_batch(tickers, period="5d", interval="1d")
    cands = {}
    for t, df in daily.items():
        today = df[[x.date() == d for x in df.index]]
        info = prev_info(df["Close"][[x.date() < d for x in df.index]].tolist())
        if len(today) and info and info[1] and float(today["High"].iloc[0]) >= CFG["min_price"] \
                and float(today["High"].iloc[0]) / info[0] - 1 >= CFG["surge"]:
            cands[t] = (info, float(today["Close"].iloc[0]))       # 최근 2일 급등 없음 & 오늘 고가 +20%
    print(f"[2/3] 후보 {len(cands)}개 (최근 2일 급등 없음, 오늘 고가 +20% 이상, $2 이상) 1분봉 확인")
    mins = download_batch(sorted(cands), chunk=50, period="5d", interval="1m", prepost=False) if cands else {}
    found = []
    for t, ((pc, fresh), close_d) in cands.items():
        m = mins.get(t)
        m = m[m.index.date == d] if m is not None else None
        if m is None or len(m) < 30 or not data_ok(m, close_d):
            continue
        for s in find_signals(t, d, m, pc, fresh):
            found.append((t, s))
    print(f"[3/3] 시그널 {len(found)}개")
    if not found:
        return f"📭 오늘은 시그널 없음\n{d} (뉴욕) | {src} 중 후보 {len(cands)}개 확인"
    lines = [f"📈 {d} (뉴욕) 시그널 {len(found)}개 | {src} 중 후보 {len(cands)}개 확인"]
    for t, s in sorted(found, key=lambda x: x[1]["time"]):
        lines.append(f"• {t} {names.get(t) or company_name(t)}\n"
                     f"   {d} {s['time']} | 종가 ${s['close']:.2f} ({s['chg']:+.1f}%)")
    return "\n".join(lines)


def company_name(t):
    """시그널 난 종목만 회사명 조회 (실패하면 빈칸)"""
    try:
        return (_yf().Ticker(t).info.get("shortName") or "")[:40]
    except Exception:
        return ""


def scan_codes(codes):
    """지정 종목: 최근 약 30일(yfinance 1분봉 한도) 시그널 날짜 목록 -> 텔레그램 메시지 문자열"""
    yf = _yf()
    end = datetime.now()
    lines = [f"🔎 종목 시그널 조회 (최근 약 30일, {len(codes)}종목)"]
    for code in codes:
        tk = yf.Ticker(code)
        daily = tk.history(period="3mo", interval="1d", auto_adjust=True)
        frames, w = [], end - timedelta(days=29)
        while w < end:                                             # 1분봉은 한 번에 최대 8일 -> 7일씩
            e = min(w + timedelta(days=7), end)
            try:
                x = tk.history(start=w, end=e, interval="1m", auto_adjust=True, prepost=False)
                if not x.empty:
                    frames.append(x)
            except Exception as ex:
                print(f"  ! {code} 1분봉 {w:%m/%d}~ 실패: {ex}")
            w = e
        if daily.empty or not frames:
            lines.append(f"• {code}: 데이터 없음 (종목 코드 확인)")
            continue
        mm = pd.concat(frames)
        mm = mm[~mm.index.duplicated()].sort_index()
        days, hits = sorted(set(mm.index.date)), []
        for d in days:
            m = mm[mm.index.date == d]
            info = prev_info(daily["Close"][[x.date() < d for x in daily.index]].tolist())
            today = daily["Close"][[x.date() == d for x in daily.index]]
            if not info or len(m) < 30 or (len(today) and not data_ok(m, float(today.iloc[0]))):
                continue
            hits += [(d, s) for s in find_signals(code, d, m, *info)]
        print(f"  {code}: {len(days)}일 확인, 시그널 {len(hits)}일")
        if not hits:
            lines.append(f"• {code}: 시그널 없음 ({days[0]} ~ {days[-1]}, {len(days)}일)")
            continue
        lines.append(f"• {code}: 시그널 {len(hits)}일 ({days[0]} ~ {days[-1]})")
        lines += [f"   {d} {s['time']} | 종가 ${s['close']:.2f} ({s['chg']:+.1f}%)" for d, s in hits]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="미국 급등주 단타 스캐너 (텔레그램 결과 전송) + 모의투자")
    ap.add_argument("--codes", nargs="+", help="조회할 종목 (예: --codes AAPL TSLA  또는  --codes AAPL,TSLA)")
    ap.add_argument("--list", action="store_true", help="전체 스캔을 실시간 전체 목록 대신 TICKERS 고정 리스트로")
    ap.add_argument("--paper", action="store_true", help="실시간 모의투자 봇 실행")
    ap.add_argument("--capital", type=float, default=CFG["capital"], help="모의투자 계좌 금액 ($)")
    ap.add_argument("--test-alert", action="store_true", help="휴대폰 알림 테스트만 보내고 종료")
    args = ap.parse_args()
    if args.test_alert:
        test_alert()
        return
    if args.paper:
        cfg = {**CFG, "capital": args.capital}
        now = datetime.now(KST)
        print(f"현재 한국시간 {now:%Y-%m-%d %H:%M}, 뉴욕 {now.astimezone(ET):%H:%M}. 모의투자 시작 (중지: Ctrl+C)")
        run_session(LiveFeed(), cfg)
        return

    # 한 번 실행 스캔: 결과·오류와 상관없이 텔레그램 메시지 1개 이상
    codes = [c.strip().upper().replace(".", "-") for a in (args.codes or []) for c in a.split(",") if c.strip()]
    try:
        msg = scan_codes(codes) if codes else scan_today(use_list=args.list)
    except Exception as e:
        traceback.print_exc()
        msg = f"⚠️ 스캔 중 오류가 나서 결과를 만들지 못했습니다\n{type(e).__name__}: {e}"
    print("\n" + msg)
    if not send_telegram(msg):
        sys.exit(1)
    print("\n텔레그램 전송 완료")


if __name__ == "__main__":
    main()
