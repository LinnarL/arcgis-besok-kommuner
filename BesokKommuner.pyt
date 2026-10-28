# -*- coding: utf-8 -*-
"""
Besök kommuner: planerar den körbara rutt mellan två punkter som går in i flest
nya kommuner inom en given tid.

Datakällor
----------
Restider: OSRM (Open Source Routing Machine), bilprofil, på OpenStreetMap-data. Restiden
  följer vägarnas skyltade hastighet och vägtyp, så en motorväg är snabbare än en
  landsväg. Standardservern är den publika demoservern och är till för måttlig
  användning. Adressen kan bytas mot en egen OSRM-instans.
    Matris:  {url}/table/v1/driving/{lon,lat;lon,lat;...}?sources=0;1&destinations=2;3
             &annotations=duration           (sekunder, null där ingen väg finns)
    Rutt:    {url}/route/v1/driving/{lon,lat;...}?overview=full&geometries=geojson
Kommungränser: om inget lager anges hämtas generaliserade gränser (290 kommuner,
  GeoJSON i WGS 84, fältet id = kommunkod) från
  https://raw.githubusercontent.com/okfse/sweden-geojson/master/swedish_municipalities.geojson
  och sparas i %LOCALAPPDATA%\\BesokKommuner. Namn och koder i listan KOMMUNER kommer från
  SCB (tabellen Folkmängd, variabeln Region).

Metod
-----
Varje kommun representeras av en punkt inuti polygonen (labelPoint). Restiderna mellan
alla dessa punkter (290 x 290) hämtas en gång och sparas i cache. Sedan löses ett
orienteringsproblem: välj och ordna kommuner så att antalet nya kommuner blir störst
medan summan av körtid och uppehåll ryms i tidsramen. Det görs med billigaste insättning,
2-opt, flytt av enskilda stopp och iterativ lokal sökning. Slutligen hämtas den verkliga
rutten från OSRM och skärs mot kommunpolygonerna, så att kommuner som rutten passerar på
vägen också räknas.
"""

import hashlib
import json
import os
import random
import re
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from xml.sax.saxutils import escape

import arcpy
import numpy as np


# =============================================================================
# Konstanter
# =============================================================================

OSRM_URL = "https://router.project-osrm.org"
KOMMUN_URL = ("https://raw.githubusercontent.com/okfse/sweden-geojson/master/"
              "swedish_municipalities.geojson")
USER_AGENT = "arcgis-besok-kommuner (ArcGIS Pro toolbox)"
SWEREF99TM_WKID = 3006
WGS84_WKID = 4326
HTTP_TIMEOUT = 120
HTTP_RETRIES = 5
TABLE_BLOCKS = (90, 50, 25)       # källor x mål per matrisanrop, minskas om servern nekar
STALL_LIMIT = 20000              # förbättringsvarv i rad utan ny bästa rutt innan sökningen avslutas
MIN_PASS_M = 10.0                 # så lång sträcka (m) ska rutten ha i en kommun för att räknas
DEFAULT_SOLVER_SECONDS = 30
_SYNC_HINTS = ("onedrive", "sharepoint", "dropbox", "google drive")

STATUS_STOP = "Nytt stopp"
STATUS_PASS = "Nytt, passeras"
STATUS_ENDS = "Nytt, start/slut"
STATUS_DONE = "Redan besökt"
STATUS_NONE = "Inte besökt"

# (kommunkod, namn), sorterat på namn med å, ä, ö sist.
KOMMUNER = (
    ("1440", "Ale"), ("1489", "Alingsås"), ("0764", "Alvesta"), ("0604", "Aneby"),
    ("1984", "Arboga"), ("2506", "Arjeplog"), ("2505", "Arvidsjaur"), ("1784", "Arvika"),
    ("1882", "Askersund"), ("2084", "Avesta"), ("1460", "Bengtsfors"), ("2326", "Berg"),
    ("2403", "Bjurholm"), ("1260", "Bjuv"), ("2582", "Boden"), ("1443", "Bollebygd"),
    ("2183", "Bollnäs"), ("0885", "Borgholm"), ("2081", "Borlänge"), ("1490", "Borås"),
    ("0127", "Botkyrka"), ("0560", "Boxholm"), ("1272", "Bromölla"), ("2305", "Bräcke"),
    ("1231", "Burlöv"), ("1278", "Båstad"), ("1438", "Dals-Ed"), ("0162", "Danderyd"),
    ("1862", "Degerfors"), ("2425", "Dorotea"), ("1730", "Eda"), ("0125", "Ekerö"),
    ("0686", "Eksjö"), ("0862", "Emmaboda"), ("0381", "Enköping"), ("0484", "Eskilstuna"),
    ("1285", "Eslöv"), ("1445", "Essunga"), ("1982", "Fagersta"), ("1382", "Falkenberg"),
    ("1499", "Falköping"), ("2080", "Falun"), ("1782", "Filipstad"), ("0562", "Finspång"),
    ("0482", "Flen"), ("1763", "Forshaga"), ("1439", "Färgelanda"), ("2026", "Gagnef"),
    ("0662", "Gislaved"), ("0461", "Gnesta"), ("0617", "Gnosjö"), ("0980", "Gotland"),
    ("1764", "Grums"), ("1444", "Grästorp"), ("1447", "Gullspång"), ("2523", "Gällivare"),
    ("2180", "Gävle"), ("1480", "Göteborg"), ("1471", "Götene"), ("0643", "Habo"),
    ("1783", "Hagfors"), ("1861", "Hallsberg"), ("1961", "Hallstahammar"), ("1380", "Halmstad"),
    ("1761", "Hammarö"), ("0136", "Haninge"), ("2583", "Haparanda"), ("0331", "Heby"),
    ("2083", "Hedemora"), ("1283", "Helsingborg"), ("1466", "Herrljunga"), ("1497", "Hjo"),
    ("2104", "Hofors"), ("0126", "Huddinge"), ("2184", "Hudiksvall"), ("0860", "Hultsfred"),
    ("1315", "Hylte"), ("0305", "Håbo"), ("1863", "Hällefors"), ("2361", "Härjedalen"),
    ("2280", "Härnösand"), ("1401", "Härryda"), ("1293", "Hässleholm"), ("1284", "Höganäs"),
    ("0821", "Högsby"), ("1266", "Hörby"), ("1267", "Höör"), ("2510", "Jokkmokk"),
    ("0123", "Järfälla"), ("0680", "Jönköping"), ("2514", "Kalix"), ("0880", "Kalmar"),
    ("1446", "Karlsborg"), ("1082", "Karlshamn"), ("1883", "Karlskoga"), ("1080", "Karlskrona"),
    ("1780", "Karlstad"), ("0483", "Katrineholm"), ("1715", "Kil"), ("0513", "Kinda"),
    ("2584", "Kiruna"), ("1276", "Klippan"), ("0330", "Knivsta"), ("2282", "Kramfors"),
    ("1290", "Kristianstad"), ("1781", "Kristinehamn"), ("2309", "Krokom"), ("1881", "Kumla"),
    ("1384", "Kungsbacka"), ("1960", "Kungsör"), ("1482", "Kungälv"), ("1261", "Kävlinge"),
    ("1983", "Köping"), ("1381", "Laholm"), ("1282", "Landskrona"), ("1860", "Laxå"),
    ("1814", "Lekeberg"), ("2029", "Leksand"), ("1441", "Lerum"), ("0761", "Lessebo"),
    ("0186", "Lidingö"), ("1494", "Lidköping"), ("1462", "Lilla Edet"), ("1885", "Lindesberg"),
    ("0580", "Linköping"), ("0781", "Ljungby"), ("2161", "Ljusdal"), ("1864", "Ljusnarsberg"),
    ("1262", "Lomma"), ("2085", "Ludvika"), ("2580", "Luleå"), ("1281", "Lund"),
    ("2481", "Lycksele"), ("1484", "Lysekil"), ("1280", "Malmö"), ("2023", "Malung-Sälen"),
    ("2418", "Malå"), ("1493", "Mariestad"), ("1463", "Mark"), ("0767", "Markaryd"),
    ("1461", "Mellerud"), ("0586", "Mjölby"), ("2062", "Mora"), ("0583", "Motala"),
    ("0642", "Mullsjö"), ("1430", "Munkedal"), ("1762", "Munkfors"), ("1481", "Mölndal"),
    ("0861", "Mönsterås"), ("0840", "Mörbylånga"), ("0182", "Nacka"), ("1884", "Nora"),
    ("1962", "Norberg"), ("2132", "Nordanstig"), ("2401", "Nordmaling"), ("0581", "Norrköping"),
    ("0188", "Norrtälje"), ("2417", "Norsjö"), ("0881", "Nybro"), ("0140", "Nykvarn"),
    ("0480", "Nyköping"), ("0192", "Nynäshamn"), ("0682", "Nässjö"), ("2101", "Ockelbo"),
    ("1060", "Olofström"), ("2034", "Orsa"), ("1421", "Orust"), ("1273", "Osby"),
    ("0882", "Oskarshamn"), ("2121", "Ovanåker"), ("0481", "Oxelösund"), ("2521", "Pajala"),
    ("1402", "Partille"), ("1275", "Perstorp"), ("2581", "Piteå"), ("2303", "Ragunda"),
    ("2409", "Robertsfors"), ("1081", "Ronneby"), ("2031", "Rättvik"), ("1981", "Sala"),
    ("0128", "Salem"), ("2181", "Sandviken"), ("0191", "Sigtuna"), ("1291", "Simrishamn"),
    ("1265", "Sjöbo"), ("1495", "Skara"), ("2482", "Skellefteå"), ("1904", "Skinnskatteberg"),
    ("1264", "Skurup"), ("1496", "Skövde"), ("2061", "Smedjebacken"), ("2283", "Sollefteå"),
    ("0163", "Sollentuna"), ("0184", "Solna"), ("2422", "Sorsele"), ("1427", "Sotenäs"),
    ("1230", "Staffanstorp"), ("1415", "Stenungsund"), ("0180", "Stockholm"),
    ("1760", "Storfors"), ("2421", "Storuman"), ("0486", "Strängnäs"), ("1486", "Strömstad"),
    ("2313", "Strömsund"), ("0183", "Sundbyberg"), ("2281", "Sundsvall"), ("1766", "Sunne"),
    ("1907", "Surahammar"), ("1214", "Svalöv"), ("1263", "Svedala"), ("1465", "Svenljunga"),
    ("1785", "Säffle"), ("2082", "Säter"), ("0684", "Sävsjö"), ("2182", "Söderhamn"),
    ("0582", "Söderköping"), ("0181", "Södertälje"), ("1083", "Sölvesborg"), ("1435", "Tanum"),
    ("1472", "Tibro"), ("1498", "Tidaholm"), ("0360", "Tierp"), ("2262", "Timrå"),
    ("0763", "Tingsryd"), ("1419", "Tjörn"), ("1270", "Tomelilla"), ("1737", "Torsby"),
    ("0834", "Torsås"), ("1452", "Tranemo"), ("0687", "Tranås"), ("1287", "Trelleborg"),
    ("1488", "Trollhättan"), ("0488", "Trosa"), ("0138", "Tyresö"), ("0160", "Täby"),
    ("1473", "Töreboda"), ("1485", "Uddevalla"), ("1491", "Ulricehamn"), ("2480", "Umeå"),
    ("0114", "Upplands Väsby"), ("0139", "Upplands-Bro"), ("0380", "Uppsala"),
    ("0760", "Uppvidinge"), ("0584", "Vadstena"), ("0665", "Vaggeryd"),
    ("0563", "Valdemarsvik"), ("0115", "Vallentuna"), ("2021", "Vansbro"), ("1470", "Vara"),
    ("1383", "Varberg"), ("0187", "Vaxholm"), ("1233", "Vellinge"), ("0685", "Vetlanda"),
    ("2462", "Vilhelmina"), ("0884", "Vimmerby"), ("2404", "Vindeln"), ("0428", "Vingåker"),
    ("1442", "Vårgårda"), ("1487", "Vänersborg"), ("2460", "Vännäs"), ("0120", "Värmdö"),
    ("0683", "Värnamo"), ("0883", "Västervik"), ("1980", "Västerås"), ("0780", "Växjö"),
    ("0512", "Ydre"), ("1286", "Ystad"), ("1492", "Åmål"), ("2260", "Ånge"), ("2321", "Åre"),
    ("1765", "Årjäng"), ("2463", "Åsele"), ("1277", "Åstorp"), ("0561", "Åtvidaberg"),
    ("0765", "Älmhult"), ("2039", "Älvdalen"), ("0319", "Älvkarleby"), ("2560", "Älvsbyn"),
    ("1292", "Ängelholm"), ("1407", "Öckerö"), ("0509", "Ödeshög"), ("1880", "Örebro"),
    ("1257", "Örkelljunga"), ("2284", "Örnsköldsvik"), ("2380", "Östersund"),
    ("0117", "Österåker"), ("0382", "Östhammar"), ("1256", "Östra Göinge"),
    ("2513", "Överkalix"), ("2518", "Övertorneå"),
)

_NAME_BY_CODE = dict(KOMMUNER)
_CODE_BY_NAME = {n.lower(): c for c, n in KOMMUNER}
VISITED_LABELS = ["{} ({})".format(n, c) for c, n in KOMMUNER]

TOOL_SUMMARY = (
    "Planerar den körbara rutt mellan en start- och en slutpunkt som går in i flest nya "
    "kommuner inom en tidsram. Passar för kommunfångst. Restiderna kommer från "
    "OpenStreetMap-vägnätet via OSRM och tar hänsyn till vägarnas hastighet, så motorvägar "
    "lönar sig framför små vägar.\n"
    "Så använder du verktyget:\n"
    "1. Välj eller rita en startpunkt. Ange en slutpunkt om rutten ska sluta på ett annat "
    "ställe. Lämnar du slutpunkten tom planeras en rundtur som börjar och slutar i "
    "startpunkten.\n"
    "2. Ange tidsramen i timmar. Körtid och uppehåll ingår. Lämnar du tidsramen tom planeras "
    "i stället den kortaste rutten som tar alla kommuner du inte redan besökt, vilket kan "
    "bli flera dygn.\n"
    "3. Kryssa i de kommuner du redan har besökt, eller peka ut en tabell med kommunkoder "
    "eller kommunnamn. Besökta kommuner ger ingen poäng, men rutten får köra genom dem.\n"
    "4. Välj var rutten och kommunlagret ska sparas. Vill du ha rutten i GPS eller mobil "
    "anger du också en GPX-fil.\n"
    "5. Kör. Första gången hämtas restider mellan alla kommuner (cirka 20 sekunder) och sparas "
    "i en cache, så att senare körningar går snabbare. Längre beräkningstid under Alternativ "
    "kan ge fler kommuner eller en kortare rutt.\n"
    "Resultat: rutten som en linje, och alla kommuner med status Nytt stopp, Nytt, passeras, "
    "Nytt, start/slut, Redan besökt eller Inte besökt. Stoppen har ordningsnummer och "
    "ankomsttid. En kommun räknas som besökt när rutten går minst 10 m in i den.\n"
    "Att tänka på: restiden är modellens, utan trafik, rast och väntetid; justera med "
    "hastighetsprocenten och uppehållstiden. Färjor ingår bara om de finns i "
    "OpenStreetMap. Standardgränserna för kommunerna är generaliserade, ange ett eget "
    "kommunlager under Datakällor för exakta gränser. Den publika OSRM-servern är till för "
    "måttlig användning."
)

TOOLTIPS = {
    "start": (
        "Var rutten börjar. Välj ett punktlager eller rita en punkt i kartan. Om flera "
        "punkter finns används den första. Kommunen där punkten ligger räknas som besökt."
    ),
    "end": (
        "Var rutten slutar. Lämnas tom används bara startpunkten: rutten börjar och slutar då "
        "där och går ut i så många nya kommuner som tidsramen räcker till innan den vänder "
        "hem igen (rundtur). Kommunen där punkten ligger räknas som besökt."
    ),
    "time_h": (
        "Hur lång tid rutten får ta, i timmar, räknat från start till slut. Körtiden och "
        "uppehållen ingår. Verktyget letar efter den rutt som ger flest nya kommuner inom "
        "tiden. Intervall 0,25 till 96 timmar. Lämnas tom planeras i stället den kortaste "
        "rutten som tar alla kommuner du inte redan besökt (kan bli flera dygn). Längre "
        "beräkningstid ger då en kortare rutt."
    ),
    "visited": (
        "Kommuner du redan har varit i. De ger ingen poäng, och rutten planeras inte för att "
        "besöka dem, men den kan köra genom dem. Kryssa i de du varit i. Kan kombineras med "
        "en tabell nedan, då gäller båda."
    ),
    "visited_table": (
        "Valfri tabell med besökta kommuner, till exempel från Excel eller en geodatabas. "
        "Den läggs till kryssrutornas val. Raderna kan vara kommunkoder (0180) eller "
        "kommunnamn (Stockholm). Rader som inte känns igen listas som varning."
    ),
    "visited_field": (
        "Fältet i tabellen som innehåller kommunkod eller kommunnamn. Fältet föreslås "
        "automatiskt när ett fält heter något i stil med kommunkod, kommun eller namn."
    ),
    "out_route": (
        "Utdata: en linjeklass med den planerade rutten som en enda linje, med total tid, "
        "körtid, längd och antal nya kommuner som attribut. Skrivs över om miljön "
        "Skriv över utdata är på."
    ),
    "out_kommuner": (
        "Utdata: en polygonklass med alla kommuner och deras status (nytt stopp, nytt och "
        "passeras, redan besökt, inte besökt). Stoppen har ordningsnummer och ankomsttid "
        "i minuter från start. Skrivs över om miljön Skriv över utdata är på."
    ),
    "out_gpx": (
        "Valfri GPX-fil för GPS eller mobilapp. Filen innehåller dels en rutt (rte) med start, "
        "alla stopp och slut som namngivna punkter, dels ett spår (trk) med hela den "
        "körda vägen. Spåret följer exakt de vägar som planerats, rutten låter en navigator "
        "räkna om mellan stoppen. Använd det som din enhet stödjer. Befintlig fil skrivs över."
    ),
    "service_min": (
        "Tid i minuter som läggs på för varje kommun du stannar i, till exempel för att "
        "fika. Standard 0: det räcker att köra in i kommunen. Tiden räknas av från "
        "tidsramen och ger färre stopp."
    ),
    "speed_pct": (
        "Hur fort du kör jämfört med vägnätets modellerade hastighet, i procent. 100 är som "
        "modellen. 90 betyder 10 procent längre körtid, till exempel för trafik eller dragvagn. "
        "Raster 50 till 150, standard 100."
    ),
    "solver_s": (
        "Hur länge verktyget får förbättra rutten, i sekunder. Längre tid ger ofta några "
        "kommuner till. Restiderna hämtas separat och ingår inte. Standard 30."
    ),
    "osrm_url": (
        "Adress till OSRM-servern med bilprofil. Standard är den publika demoservern, som är "
        "till för måttlig användning: en första körning gör cirka 20 anrop och därefter "
        "används en cache. För regelbunden användning, kör en egen OSRM-instans och ange "
        "dess adress här."
    ),
    "kommun_layer": (
        "Valfritt polygonlager med kommungränser. Lämnas tomt hämtas generaliserade gränser "
        "(cirka 1 km noggrannhet) från GitHub och sparas i cache. Lagret styr: ett urval "
        "eller en definitionsfråga begränsar vilka kommuner som får vara med. Kommuner som "
        "saknas i lagret kan inte besökas."
    ),
    "kommun_field": (
        "Fältet i kommunlagret som innehåller kommunkod (0180) eller kommunnamn. Krävs "
        "när ett eget kommunlager anges."
    ),
}


# =============================================================================
# Förlopp
# =============================================================================

def _fmt_duration(seconds):
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return "{} s".format(seconds)
    if seconds < 3600:
        return "{} min".format(int(round(seconds / 60.0)))
    return "{} h {} min".format(seconds // 3600, (seconds % 3600) // 60)


def _fmt_budget(budget):
    return _fmt_duration(budget) if budget != float("inf") else "obegränsad"


def _fmt_num(v, decimals=1):
    """Decimalkomma och tusentalsmellanrum: 1 234,5."""
    return "{:,.{d}f}".format(float(v), d=decimals).replace(",", " ").replace(".", ",")


class _Steps:
    """Numrerade steg i förloppsindikatorn och i meddelandena."""

    def __init__(self, total, messages):
        self.total = total
        self.messages = messages
        self.k = 0
        self.t_step = time.time()

    def next(self, text):
        self.k += 1
        self.t_step = time.time()
        label = "Steg {} av {}: {}".format(self.k, self.total, text)
        arcpy.SetProgressor("default", label)
        self.messages.addMessage(label)

    def label(self, text):
        arcpy.SetProgressorLabel("Steg {} av {}: {}".format(self.k, self.total, text))

    def done(self, text=None):
        msg = "    klart på {}".format(_fmt_duration(time.time() - self.t_step))
        self.messages.addMessage(msg + (". " + text if text else "."))


# =============================================================================
# Kommuner: namn, koder och polygoner
# =============================================================================

def _resolve_kommun(value):
    """Kommunkod för ett värde som kod (0180, 180), namn (Stockholm, Stockholms kommun)
    eller "Namn (kod)". None om det inte känns igen."""
    if value is None:
        return None
    t = str(value).strip()
    if not t:
        return None
    m = re.search(r"\((\d{3,4})\)\s*$", t)
    if m:
        t = m.group(1)
    try:
        f = float(t)
        if f == int(f) and f >= 0:
            t = str(int(f))
    except ValueError:
        pass
    if t.isdigit():
        code = t.zfill(4)
        return code if code in _NAME_BY_CODE else None
    k = t.lower()
    if k in _CODE_BY_NAME:
        return _CODE_BY_NAME[k]
    for suffix in (" kommun", " stad"):             # "Stockholms kommun", "Malmö stad"
        if k.endswith(suffix):
            k = k[:-len(suffix)].strip()
    return _CODE_BY_NAME.get(k) or _CODE_BY_NAME.get(k[:-1] if k.endswith("s") else k)


def _cache_dir():
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = os.path.join(base, "BesokKommuner")
    os.makedirs(path, exist_ok=True)
    return path


def _http_bytes(url):
    """GET med omförsök vid tillfälliga fel. HTTPError med kod 400-499 (utom 408, 429)
    kastas direkt så att anroparen kan reagera, till exempel minska matrisblocket."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    delay = 2
    for attempt in range(HTTP_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            # HTTPError först: den är också URLError och OSError.
            if exc.code in (408, 429) or exc.code >= 500:
                if attempt < HTTP_RETRIES - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
            raise
        except (urllib.error.URLError, OSError):
            if attempt < HTTP_RETRIES - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


def _kommuner_from_layer(layer, field):
    """Kommuner ur ett polygonlager, via lagret så att urval och definitionsfråga gäller."""
    sr = arcpy.SpatialReference(SWEREF99TM_WKID)
    sr_in = arcpy.Describe(layer).spatialReference
    if sr_in is None or not (sr_in.factoryCode or sr_in.exportToString()):
        raise ValueError("Kommunlagret saknar koordinatsystem.")
    shapes = {}
    unknown = set()
    with arcpy.da.SearchCursor(layer, [field, "SHAPE@"], spatial_reference=sr) as cur:
        for value, shape in cur:
            if shape is None:
                continue
            code = _resolve_kommun(value)
            if code is None:
                unknown.add(str(value))
                continue
            shapes[code] = shape if code not in shapes else shapes[code].union(shape)
    if not shapes:
        raise ValueError(
            "Inga kommuner i lagret kunde kopplas till en kommunkod eller ett kommunnamn "
            "via fältet {}.".format(field))
    return shapes, sorted(unknown)


def _kommuner_from_download(messages):
    """De generaliserade gränserna från GitHub, hämtade en gång och sedan från cache."""
    path = os.path.join(_cache_dir(), "kommuner.geojson")
    if not os.path.exists(path):
        messages.addMessage("    Hämtar kommungränser (cirka 0,8 MB).")
        try:
            data = _http_bytes(KOMMUN_URL)
        except (urllib.error.URLError, OSError) as exc:
            raise ValueError(
                "Kunde inte hämta kommungränser från GitHub ({}). Ange ett eget kommunlager "
                "under Datakällor.".format(exc))
        with open(path, "wb") as fh:
            fh.write(data)
    with open(path, encoding="utf-8") as fh:
        collection = json.load(fh)
    sr_out = arcpy.SpatialReference(SWEREF99TM_WKID)
    shapes = {}
    for feat in collection["features"]:
        code = _resolve_kommun(feat["properties"].get("id"))
        if code is None:
            continue
        shape = arcpy.AsShape(feat["geometry"], False)   # GeoJSON, WGS 84
        shapes[code] = shape.projectAs(sr_out)
    return shapes


def _load_kommuner(layer, field, messages):
    """Lista med en post per kommun: kod, namn, shape (SWEREF 99 TM) och lon, lat för en
    punkt inuti kommunen. Sorterad på kod, så att cachenyckeln blir stabil."""
    if layer:
        shapes, unknown = _kommuner_from_layer(layer, field)
        messages.addMessage("    {} kommuner från lagret.".format(len(shapes)))
        if unknown:
            messages.addWarningMessage(
                "Värden i fältet {} som inte är en känd kommun hoppades över: {}".format(
                    field, ", ".join(unknown[:10]) + (" ..." if len(unknown) > 10 else "")))
    else:
        shapes = _kommuner_from_download(messages)
        messages.addMessage("    {} kommuner (generaliserade gränser).".format(len(shapes)))
    sr = arcpy.SpatialReference(SWEREF99TM_WKID)
    wgs = arcpy.SpatialReference(WGS84_WKID)
    out = []
    for code in sorted(shapes):
        shape = shapes[code]
        pt = arcpy.PointGeometry(shape.labelPoint, sr)
        ll = pt.projectAs(wgs).firstPoint
        out.append({"kod": code, "namn": _NAME_BY_CODE[code], "shape": shape,
                    "lon": ll.X, "lat": ll.Y})
    return out


# =============================================================================
# Start- och slutpunkt
# =============================================================================

def _point_schema():
    """Tom punktfeatureklass i SWEREF 99 TM som standardvärde för Feature Set-parametern,
    så att ritverktyget ritar punkter. Unikt namn: memory delas av hela Pro-sessionen."""
    name = "punkt_schema_{}".format(uuid.uuid4().hex[:12])
    arcpy.management.CreateFeatureclass("memory", name, "POINT",
                                        spatial_reference=arcpy.SpatialReference(SWEREF99TM_WKID))
    return "memory/" + name


def _is_layer(value):
    # arcpy.mp har ingen Layer-klass att jämföra med; klassen ligger i arcpy._mp.
    return type(value).__name__ == "Layer"


def _has_features(value):
    """True om värdet har minst ett objekt (ett lagers urval räknas), False om tomt, None
    om det inte går att läsa. Läser bara första raden."""
    try:
        with arcpy.da.SearchCursor(value, ["OID@"]) as cur:
            return next(iter(cur), None) is not None
    except Exception:
        return None


def _read_point(value, label, messages):
    """Första punkten i en Feature Set-parameter, som PointGeometry i SWEREF 99 TM."""
    if _has_features(value) is False:
        raise ValueError("{}: rita eller välj en punkt.".format(label))
    sr_in = arcpy.Describe(value).spatialReference
    if sr_in is None or not (sr_in.factoryCode or sr_in.exportToString()):
        raise ValueError("{} saknar koordinatsystem.".format(label))
    sr = arcpy.SpatialReference(SWEREF99TM_WKID)
    points = []
    with arcpy.da.SearchCursor(value, ["SHAPE@"], spatial_reference=sr) as cur:
        for (shape,) in cur:
            if shape is not None and shape.firstPoint is not None:
                points.append(shape)
    if not points:
        raise ValueError("{}: ingen punkt hittades.".format(label))
    if len(points) > 1:
        messages.addWarningMessage(
            "{}: {} punkter hittades, den första används.".format(label, len(points)))
    return arcpy.PointGeometry(points[0].firstPoint, sr)


def _lonlat(point):
    ll = point.projectAs(arcpy.SpatialReference(WGS84_WKID)).firstPoint
    return ll.X, ll.Y


# =============================================================================
# Restider (OSRM)
# =============================================================================

class _TooBig(Exception):
    """Servern nekade matrisblocket: försök med ett mindre."""


def _coords(points):
    return ";".join("{:.6f},{:.6f}".format(x, y) for x, y in points)


def _table_block(base, src, dst):
    """Restider i sekunder, len(src) x len(dst), NaN där ingen väg finns."""
    n = len(src)
    url = "{}/table/v1/driving/{}?sources={}&destinations={}&annotations=duration".format(
        base, _coords(list(src) + list(dst)),
        ";".join(str(i) for i in range(n)), ";".join(str(n + j) for j in range(len(dst))))
    try:
        body = _http_bytes(url)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 413, 414):
            raise _TooBig()
        raise
    res = json.loads(body.decode("utf-8"))
    if res.get("code") != "Ok":
        raise ValueError("OSRM svarade {}: {}".format(res.get("code"), res.get("message", "")))
    return np.array([[np.nan if v is None else v for v in row] for row in res["durations"]],
                    dtype=np.float64)


def _table(base, src, dst, steps=None, label="Restider"):
    """Restidsmatris src x dst, uppdelad i block. Minskar blockstorleken om servern nekar."""
    last = None
    for size in TABLE_BLOCKS:
        rows = [(i, min(i + size, len(src))) for i in range(0, len(src), size)]
        cols = [(j, min(j + size, len(dst))) for j in range(0, len(dst), size)]
        total = len(rows) * len(cols)
        out = np.full((len(src), len(dst)), np.nan)
        if steps is not None and total > 1:
            arcpy.SetProgressor("step", label, 0, total, 1)
        t0 = time.time()
        done = 0
        try:
            for a, b in rows:
                for c, d in cols:
                    out[a:b, c:d] = _table_block(base, src[a:b], dst[c:d])
                    done += 1
                    if steps is not None and total > 1:
                        left = (time.time() - t0) / done * (total - done)
                        text = "{} {} av {}{}".format(
                            label, done, total, ", ca {} kvar".format(_fmt_duration(left))
                            if done >= 2 and left >= 30 else "")
                        arcpy.SetProgressorLabel(text)
                        arcpy.SetProgressorPosition()
                        steps.messages.addMessage("    " + text)
                    if base == OSRM_URL:
                        time.sleep(0.3)         # den publika servern är till för måttlig last
            return out
        except _TooBig as exc:
            last = exc
            continue
        except urllib.error.HTTPError as exc:
            raise ValueError("Routingtjänsten svarade HTTP {} ({}).".format(exc.code, exc.reason))
        except (urllib.error.URLError, OSError) as exc:
            raise ValueError("Kunde inte nå routingtjänsten på {} ({}).".format(base, exc))
    raise ValueError("Routingtjänsten godtog inte ens små matrisanrop. Kontrollera adressen.")


def _kommun_matrix(base, kommuner, steps):
    """Restider mellan alla kommunpunkter, från cache om samma punkter och server redan
    körts. Cachen ligger i %LOCALAPPDATA%, inte i den synkade projektmappen."""
    pts = [(k["lon"], k["lat"]) for k in kommuner]
    key = hashlib.sha1((base + "|" + _coords(pts)).encode("utf-8")).hexdigest()[:16]
    path = os.path.join(_cache_dir(), "matris_{}.npz".format(key))
    if os.path.exists(path):
        try:
            with np.load(path) as z:
                m = z["d"].astype(np.float64)
            if m.shape == (len(pts), len(pts)):
                steps.messages.addMessage("    Restider mellan {} kommuner från cache.".format(len(pts)))
                return m
        except Exception:
            pass
    steps.messages.addMessage(
        "    Hämtar restider mellan {} kommuner från {} (görs en gång, sparas i cache).".format(
            len(pts), base))
    m = _table(base, pts, pts, steps, "Restider kommun till kommun, block")
    np.savez_compressed(path, d=m.astype(np.float32))
    return m


ROUTE_CHUNK = 80          # mellanpunkter per ruttanrop, så att URL:en inte blir för lång


def _osrm_route(base, points):
    """Rutten genom punkterna. Många punkter delas i bitar som får gemensamma ändpunkter och
    sätts ihop: geometri, delsträckor, tid och längd."""
    if len(points) <= ROUTE_CHUNK:
        return _osrm_route_once(base, points)
    parts = [_osrm_route_once(base, points[i:i + ROUTE_CHUNK + 1])
             for i in range(0, len(points) - 1, ROUTE_CHUNK)]
    coords = []
    for part in parts:
        c = part["geometry"]["coordinates"]
        coords.extend(c[1:] if coords else c)
    return {"geometry": {"coordinates": coords},
            "legs": [l for part in parts for l in part["legs"]],
            "duration": sum(part["duration"] for part in parts),
            "distance": sum(part["distance"] for part in parts)}


def _osrm_route_once(base, points):
    # continue_straight=false: bilprofilen förbjuder annars U-sväng vid mellanpunkter, vilket
    # tabelltjänsten inte gör. Mätt: 80 716 s mot 78 646 s för samma 12 punkter.
    url = ("{}/route/v1/driving/{}?overview=full&geometries=geojson&steps=false"
           "&continue_straight=false").format(base, _coords(points))
    try:
        res = json.loads(_http_bytes(url).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ValueError("Routingtjänsten svarade HTTP {} vid ruttberäkningen.".format(exc.code))
    except (urllib.error.URLError, OSError) as exc:
        raise ValueError("Kunde inte nå routingtjänsten på {} ({}).".format(base, exc))
    if res.get("code") != "Ok":
        raise ValueError("OSRM kunde inte beräkna rutten: {} {}".format(
            res.get("code"), res.get("message", "")))
    return res["routes"][0]


# =============================================================================
# Ruttoptimering (orienteringsproblem med poäng 1 per kommun)
# =============================================================================
# Noder: 0 = start, 1 = slut, 2.. = kommuner. C[i, j] är tiden i sekunder från i till j,
# inklusive uppehållet vid j. En rutt är en lista [0, ..., 1].

def _cost(C, r):
    return float(C[r[:-1], r[1:]].sum())


def _insertion_costs(C, r, cand):
    """Extra tid för att lägga in varje kandidat på bästa plats. (plats, extra) per kandidat."""
    a = np.asarray(r[:-1])
    b = np.asarray(r[1:])
    ext = C[a][:, cand] + C[cand][:, b].T - C[a, b][:, None]
    pos = ext.argmin(axis=0)
    return pos, ext[pos, np.arange(len(cand))]


def _fill(C, budget, r, allowed, rng, pick=1):
    """Lägg till kommuner med billigaste insättning så länge tidsramen räcker. pick > 1
    väljer slumpmässigt bland de pick billigaste (för variation i sökningen)."""
    used = np.zeros(len(C), dtype=bool)
    used[r] = True
    cost = _cost(C, r)
    while True:
        cand = np.flatnonzero(allowed & ~used)
        if len(cand) == 0:
            break
        pos, val = _insertion_costs(C, r, cand)
        ok = np.flatnonzero(np.isfinite(val) & (cost + val <= budget + 1e-9))
        if len(ok) == 0:
            break
        if pick > 1 and len(ok) > 1:
            order = ok[np.argsort(val[ok])][:pick]
            c = int(order[rng.randrange(len(order))])
        else:
            c = int(ok[val[ok].argmin()])
        r.insert(int(pos[c]) + 1, int(cand[c]))
        used[cand[c]] = True
        cost += float(val[c])
    return r, cost


def _two_opt(C, r):
    """Vänd delsträckor så länge det kortar rutten. Går på O(1) per kandidat med
    prefixsummor, och stämmer även när restiderna inte är symmetriska."""
    improved = True
    while improved:
        improved = False
        a = np.asarray(r)
        m = len(a)
        if m < 4:
            break
        fw = C[a[:-1], a[1:]]
        bw = C[a[1:], a[:-1]]
        bw = np.where(np.isfinite(bw), bw, 1e12)    # ingen väg bakåt: mycket dyrt, inte inf
        cf = np.concatenate(([0.0], np.cumsum(fw)))
        cb = np.concatenate(([0.0], np.cumsum(bw)))
        for i in range(1, m - 2):
            j = np.arange(i + 1, m - 1)
            old = fw[i - 1] + (cf[j] - cf[i]) + fw[j]
            new = C[a[i - 1], a[j]] + (cb[j] - cb[i]) + C[a[i], a[j + 1]]
            delta = np.where(np.isfinite(new), new - old, np.inf)
            k = int(delta.argmin())
            if delta[k] < -1e-6:
                jj = int(j[k])
                r[i:jj + 1] = r[i:jj + 1][::-1]
                improved = True
                break
    return r


def _relocate(C, r):
    """Flytta enskilda stopp till bättre plats i rutten så länge det kortar den."""
    improved = True
    while improved:
        improved = False
        for p in range(1, len(r) - 1):
            x, a, b = r[p], r[p - 1], r[p + 1]
            gain = C[a, x] + C[x, b] - C[a, b]
            rest = r[:p] + r[p + 1:]
            ra = np.asarray(rest)
            ext = C[ra[:-1], x] + C[x, ra[1:]] - C[ra[:-1], ra[1:]]
            k = int(ext.argmin())
            if ext[k] < gain - 1e-6:
                rest.insert(k + 1, x)
                r[:] = rest
                improved = True
                break
    return r


def _local_search(C, budget, r, allowed, rng):
    while True:
        before = _cost(C, r)
        _two_opt(C, r)
        _relocate(C, r)
        n = len(r)
        r, cost = _fill(C, budget, r, allowed, rng)
        if len(r) == n and cost >= before - 1e-6:
            break
    return r, _cost(C, r)


def _solve(C, budget, allowed, seconds, rng, progress=None):
    """Bästa rutt: flest stopp, därefter kortast tid. Returnerar (rutt, kostnad)."""
    start, _ = _fill(C, budget, [0, 1], allowed, rng)
    best, best_cost = _local_search(C, budget, start, allowed, rng)
    n_allowed = int(allowed.sum())
    t0 = time.time()
    last_report = t0
    stall = 0
    iterations = 0
    unlimited = not np.isfinite(budget)      # alla kommuner tas, målet är kortast möjliga rutt
    while time.time() - t0 < seconds and (unlimited or len(best) - 2 < n_allowed) \
            and stall < STALL_LIMIT:
        iterations += 1
        r = list(best)
        stops = len(r) - 2
        if stops > 0:
            k = rng.randint(1, max(1, min(stops, 2 + stops // 6)))
            if rng.random() < 0.5:                       # ett sammanhängande stycke
                s = rng.randint(1, stops - k + 1)
                del r[s:s + k]
            else:                                        # slumpvalda stopp
                for idx in sorted(rng.sample(range(1, stops + 1), k), reverse=True):
                    del r[idx]
        r, _ = _fill(C, budget, r, allowed, rng, pick=3)
        r, cost = _local_search(C, budget, r, allowed, rng)
        if len(r) > len(best) or (len(r) == len(best) and cost < best_cost - 1e-6):
            best, best_cost, stall = r, cost, 0
        else:
            stall += 1
        if progress is not None and time.time() - last_report >= 3:
            last_report = time.time()
            progress(len(best) - 2, best_cost, time.time() - t0, seconds)
    return best, best_cost, iterations


# =============================================================================
# Verklig rutt och kommunstatus
# =============================================================================

def _route_line(route):
    """Rutten från OSRM som arcpy.Polyline i SWEREF 99 TM."""
    wgs = arcpy.SpatialReference(WGS84_WKID)
    arr = arcpy.Array([arcpy.Point(x, y) for x, y in route["geometry"]["coordinates"]])
    return arcpy.Polyline(arr, wgs).projectAs(arcpy.SpatialReference(SWEREF99TM_WKID))


def _length_in(line, shape):
    """Linjens längd i meter inom polygonen."""
    le, se = line.extent, shape.extent
    if le.XMax < se.XMin or le.XMin > se.XMax or le.YMax < se.YMin or le.YMin > se.YMax:
        return 0.0
    if line.disjoint(shape):
        return 0.0
    part = line.intersect(shape, 2)
    return float(part.length) if part is not None else 0.0


# =============================================================================
# Utdata
# =============================================================================

def _export(out_path, geometry_type, fields, rows, sr):
    """Bygg i memory och skriv med en ExportFeatures. Att bygga stegvis direkt på en
    filserver kostar 16-27 s per geobehandlingsanrop. fields: (namn, typ, alias, längd)."""
    name = "bk_{}".format(uuid.uuid4().hex[:12])
    mem = "memory/" + name
    arcpy.management.CreateFeatureclass("memory", name, geometry_type, spatial_reference=sr)
    arcpy.management.AddFields(mem, [[f[0], f[1], f[2]] + ([f[3]] if f[3] else [])
                                     for f in fields])
    with arcpy.da.InsertCursor(mem, ["SHAPE@"] + [f[0] for f in fields]) as cur:
        for row in rows:
            cur.insertRow(row)
    try:
        with arcpy.EnvManager(extent="MAXOF"):
            arcpy.conversion.ExportFeatures(mem, out_path)
    finally:
        try:
            arcpy.management.Delete(mem)
        except Exception:
            pass


def _write_gpx(path, title, via, track):
    """GPX 1.1 med en rutt (via: lista av (namn, lon, lat)) och ett spår (track: lon, lat)."""
    q = lambda t: escape(t, {'"': "&quot;"})
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="Besök kommuner" xmlns="http://www.topografix.com/GPX/1/1">',
           "<metadata><name>{}</name></metadata>".format(q(title)),
           "<rte><name>{}</name>".format(q(title))]
    for name, lon, lat in via:
        out.append('<rtept lat="{:.6f}" lon="{:.6f}"><name>{}</name></rtept>'.format(
            lat, lon, q(name)))
    out.append("</rte>")
    out.append("<trk><name>{}</name><trkseg>".format(q(title)))
    out.extend('<trkpt lat="{:.6f}" lon="{:.6f}"/>'.format(lat, lon) for lon, lat in track)
    out.append("</trkseg></trk></gpx>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))


# =============================================================================
# Körningens innehåll (separat funktion - går att testa utanför Pro)
# =============================================================================

def _run(start_pt, end_pt, hours, visited, osrm_url, kommun_layer, kommun_field,
         service_min, speed_pct, solver_s, out_route, out_kommuner, messages,
         out_gpx=None):
    """
    start_pt, end_pt: PointGeometry i SWEREF 99 TM (end_pt None = tillbaka till start).
    visited: mängd av kommunkoder som redan är besökta.
    Returnerar (antal nya kommuner, antal stopp).
    """
    steps = _Steps(5, messages)
    base = (osrm_url or OSRM_URL).strip().rstrip("/")
    budget = float("inf") if hours is None else hours * 3600.0
    service = max(0.0, service_min) * 60.0
    factor = 100.0 / max(1.0, speed_pct)
    roundtrip = end_pt is None
    if roundtrip:
        end_pt = start_pt
        messages.addMessage(
            "Ingen slutpunkt angiven: planerar en rundtur som börjar och slutar i startpunkten"
            + (" och utnyttjar tidsramen på {} h.".format(_fmt_num(hours, 1)) if hours else "."))
    if hours is None:
        messages.addMessage(
            "Ingen tidsram angiven: planerar den kortaste rutten som tar alla kommuner som "
            "inte redan är besökta.")
    elif not (0 < hours):
        raise ValueError("Tidsramen måste vara större än noll.")

    # ---- 1. Kommuner ------------------------------------------------------
    steps.next("läser kommungränser")
    kommuner = _load_kommuner(kommun_layer, kommun_field, messages)
    N = len(kommuner)
    index = {k["kod"]: i for i, k in enumerate(kommuner)}
    home = set()
    for label, pt in (("Startpunkten", start_pt), ("Slutpunkten", end_pt)):
        hit = [k for k in kommuner if k["shape"].contains(pt)]
        if hit:
            home.add(hit[0]["kod"])
            messages.addMessage("    {} ligger i {}.".format(label, hit[0]["namn"]))
        else:
            messages.addWarningMessage(
                "{} ligger utanför alla kommuner i lagret.".format(label))
    known_visited = {c for c in visited if c in index}
    messages.addMessage("    Redan besökta kommuner: {}.".format(len(known_visited)))
    steps.done()

    # ---- 2. Restider ------------------------------------------------------
    steps.next("hämtar restider")
    D = np.full((N + 2, N + 2), np.nan)
    D[2:, 2:] = _kommun_matrix(base, kommuner, steps)
    ends = [_lonlat(start_pt), _lonlat(end_pt)]
    kp = [(k["lon"], k["lat"]) for k in kommuner]
    steps.label("restider från start och slut")
    D[:2, 2:] = _table(base, ends, kp)
    D[2:, :2] = _table(base, kp, ends)
    D[:2, :2] = 0.0 if roundtrip else _table(base, ends, ends)
    steps.done()

    # ---- 3. Optimering ----------------------------------------------------
    steps.next("optimerar rutten")
    C = D * factor
    C[:, 2:] += service
    C[np.isnan(C)] = np.inf
    np.fill_diagonal(C, 0.0)
    allowed = np.zeros(N + 2, dtype=bool)
    for k in kommuner:
        i = index[k["kod"]] + 2
        if k["kod"] in known_visited or k["kod"] in home:
            continue
        allowed[i] = np.isfinite(C[0, i]) and np.isfinite(C[i, 1]) and C[0, i] + C[i, 1] <= budget
    messages.addMessage("    {} kommuner är möjliga att nå inom tidsramen och inte besökta.".format(
        int(allowed.sum())))
    cut = [k["namn"] for k in kommuner
           if k["kod"] not in known_visited and k["kod"] not in home
           and not (np.isfinite(C[0, index[k["kod"]] + 2]) and np.isfinite(C[index[k["kod"]] + 2, 1]))]
    if cut and hours is None:
        messages.addWarningMessage(
            "{} kommuner går inte att nå med bil från start till slut och hoppas över: {}".format(
                len(cut), ", ".join(cut[:10]) + (" ..." if len(cut) > 10 else "")))
    if C[0, 1] > budget:
        raise ValueError(
            "Det går inte att köra från start till slut inom tidsramen (tar {}).".format(
                _fmt_duration(C[0, 1])))

    rng = random.Random(20260101)

    def progress(count, cost, elapsed, total):
        text = "Optimerar: {} kommuner hittills, {} av {}".format(
            count, _fmt_duration(cost), _fmt_budget(budget))
        steps.label(text)

    eff_budget = budget
    for attempt in range(3):
        route, cost, iters = _solve(C, eff_budget, allowed, solver_s, rng, progress)
        stops = [kommuner[i - 2] for i in route[1:-1]]
        messages.addMessage(
            "    Rutt med {} stopp, beräknad tid {} (tidsram {}), {} förbättringsvarv.".format(
                len(stops), _fmt_duration(cost), _fmt_budget(budget), iters))
        if attempt == 0:
            steps.done()
            # ---- 4. Verklig rutt ----------------------------------------------
            steps.next("hämtar den verkliga rutten och kontrollerar kommunerna")
        steps.label("hämtar den verkliga rutten")
        waypoints = [ends[0]] + [(k["lon"], k["lat"]) for k in stops] + [ends[1]]
        osrm = _osrm_route(base, waypoints)
        legs = [l["duration"] * factor for l in osrm["legs"]]
        total = sum(legs) + service * len(stops)
        if total <= budget + 1.0:
            break
        over = total - budget
        messages.addMessage(
            "    Verklig tid {} överskrider tidsramen med {}. Planerar om med lite marginal.".format(
                _fmt_duration(total), _fmt_duration(over)))
        eff_budget -= over + 30.0
        if eff_budget <= C[0, 1]:
            break
    if total > budget + 1.0:
        messages.addWarningMessage(
            "Verklig körtid {} är längre än tidsramen {}. Minska tidsramen något.".format(
                _fmt_duration(total), _fmt_budget(budget)))

    line = _route_line(osrm)
    # Ankomsttid: körtid hittills plus uppehållen vid tidigare stopp.
    arrive = {}
    clock = 0.0
    for n, k in enumerate(stops):
        clock += legs[n]
        arrive[k["kod"]] = clock
        clock += service

    steps.label("skär rutten mot kommunerna")
    length_in = {}
    for n, k in enumerate(kommuner):
        length_in[k["kod"]] = _length_in(line, k["shape"])
    stop_order = {k["kod"]: n + 1 for n, k in enumerate(stops)}
    status = {}
    for k in kommuner:
        c = k["kod"]
        if c in known_visited:
            status[c] = STATUS_DONE
        elif c in stop_order:
            status[c] = STATUS_STOP
        elif c in home:
            status[c] = STATUS_ENDS
        elif length_in[c] >= MIN_PASS_M:
            status[c] = STATUS_PASS
        else:
            status[c] = STATUS_NONE
    n_stop = sum(1 for s in status.values() if s == STATUS_STOP)
    n_pass = sum(1 for s in status.values() if s == STATUS_PASS)
    n_ends = sum(1 for s in status.values() if s == STATUS_ENDS)
    n_new = n_stop + n_pass + n_ends
    length_km = osrm["distance"] / 1000.0
    steps.done("{} nya kommuner.".format(n_new))

    # ---- 5. Skriv utdata --------------------------------------------------
    steps.next("skriver utdata")
    sr = arcpy.SpatialReference(SWEREF99TM_WKID)
    _export(
        out_route, "POLYLINE",
        [("Tot_min", "DOUBLE", "Total tid inkl. uppehåll (min)", None),
         ("Kor_min", "DOUBLE", "Körtid (min)", None),
         ("Langd_km", "DOUBLE", "Längd (km)", None),
         ("Tidsram", "DOUBLE", "Tidsram (min)", None),
         ("Nya", "LONG", "Nya kommuner", None),
         ("Stopp", "LONG", "Antal stopp", None),
         ("Passeras", "LONG", "Passerade nya kommuner", None)],
        [(line, total / 60.0, sum(legs) / 60.0, length_km, budget / 60.0 if np.isfinite(budget) else None,
          n_new, n_stop, n_pass)],
        sr)
    rows = []
    for k in kommuner:
        c = k["kod"]
        rows.append((k["shape"], c, k["namn"], status[c], stop_order.get(c),
                     arrive[c] / 60.0 if c in arrive else None,
                     round(length_in[c]) if length_in[c] > 0 else None,
                     "Ja" if c in known_visited else "Nej"))
    _export(
        out_kommuner, "POLYGON",
        [("Kod", "TEXT", "Kommunkod", 4), ("Namn", "TEXT", "Kommun", 60),
         ("Status", "TEXT", "Status", 30), ("Ordning", "LONG", "Stopp nummer", None),
         ("Ank_min", "DOUBLE", "Ankomst efter (min)", None),
         ("Vag_m", "LONG", "Rutt i kommunen (m)", None),
         ("Tidigare", "TEXT", "Redan besökt", 3)],
        rows, sr)
    if out_gpx:
        steps.label("skriver GPX-fil")
        via = ([("Start", ends[0][0], ends[0][1])]
               + [("{}. {}".format(n + 1, k["namn"]), k["lon"], k["lat"])
                  for n, k in enumerate(stops)]
               + [("Slut", ends[1][0], ends[1][1])])
        try:
            _write_gpx(out_gpx, "Kommunrutt, {} nya kommuner".format(n_new), via,
                       osrm["geometry"]["coordinates"])
        except OSError as exc:
            raise ValueError("Kunde inte skriva GPX-filen ({}).".format(exc))
        messages.addMessage("    GPX-fil skriven: {}".format(out_gpx))
    steps.done()

    messages.addMessage(
        "Klart: {} nya kommuner ({} stopp, {} passeras, {} vid start och slut). "
        "Totalt {} på {} km, tidsram {}.".format(
            n_new, n_stop, n_pass, n_ends, _fmt_duration(total),
            _fmt_num(length_km, 0), _fmt_budget(budget)))
    if known_visited:
        messages.addMessage("Redan besökta kommuner räknas inte: {}.".format(len(known_visited)))
    return n_new, n_stop


# =============================================================================
# Verktygstips (sidecar-fil som Pro läser)
# =============================================================================

def _write_tool_metadata(tool_cls, toolbox_alias):
    """
    Skriv verktygets metadatafil med parameterförklaringar från TOOLTIPS. Pro läser tipsen
    ur <verktygslåda>.<verktyg>.pyt.xml (dialogReference per parameter). Skrivs bara om
    innehållet har ändrats.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    toolbox = os.path.splitext(os.path.basename(__file__))[0]
    path = os.path.join(here, "{}.{}.pyt.xml".format(toolbox, tool_cls.__name__))

    def html(text):
        body = escape(text).replace("\n", "</SPAN></P><P><SPAN>")
        return escape('<DIV STYLE="text-align:Left;"><P><SPAN>{}</SPAN></P></DIV>'.format(body))

    tool = tool_cls()
    params = []
    for p in tool.getParameterInfo():
        tip = TOOLTIPS.get(p.name)
        if not tip:
            continue
        params.append(
            '<param name="{n}" displayname="{d}" type="{t}" direction="{r}">'
            "<dialogReference>{h}</dialogReference>"
            "<pythonReference>{h}</pythonReference></param>".format(
                n=p.name, d=escape(p.displayName, {'"': "&quot;"}),
                t=p.parameterType, r=p.direction, h=html(tip))
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<metadata xml:lang="sv"><Esri><ArcGISFormat>1.0</ArcGISFormat></Esri>'
        '<tool name="{name}" displayname="{label}" toolboxalias="{alias}" xmlns="">'
        "<parameters>{params}</parameters><summary>{summary}</summary></tool>"
        "<dataIdInfo><idCitation><resTitle>{label}</resTitle></idCitation>"
        "<idAbs>{summary}</idAbs></dataIdInfo></metadata>\n"
    ).format(name=tool_cls.__name__, label=escape(tool.label), alias=toolbox_alias,
             params="".join(params), summary=html(TOOL_SUMMARY))
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == xml:
                return
    except OSError:
        pass
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(xml)
    except OSError:
        pass        # skrivskyddad plats: verktyget fungerar ändå, bara utan verktygstips


def _default_workspace():
    try:
        gdb = arcpy.mp.ArcGISProject("CURRENT").defaultGeodatabase
        if gdb:
            return gdb
    except Exception:
        pass
    return None


_FIELD_HINTS = ("kommunkod", "kommun_kod", "knkod", "kommunnamn", "kommun", "kn_namn",
                "kom_namn", "namn", "kod", "name")


def _field_choices(source):
    """Textfält och heltalsfält i en tabell eller ett lager, och det som liknar en kommun."""
    try:
        fields = [f.name for f in arcpy.ListFields(source)
                  if f.type in ("String", "Integer", "SmallInteger", "Double")
                  and f.name.upper() not in ("SHAPE", "OBJECTID", "FID", "OID")]
    except Exception:
        return [], None
    lower = {f.lower(): f for f in fields}
    guess = next((lower[h] for h in _FIELD_HINTS if h in lower), fields[0] if fields else None)
    return fields, guess


# =============================================================================
# Toolbox
# =============================================================================

class Toolbox:
    def __init__(self):
        self.label = "Besök kommuner"
        self.alias = "besok_kommuner"
        self.tools = [PlaneraKommunrutt]
        _write_tool_metadata(PlaneraKommunrutt, self.alias)


class PlaneraKommunrutt:
    def __init__(self):
        self.label = "Planera rutt genom flest kommuner"
        self.description = TOOL_SUMMARY
        self.canRunInBackground = False
        self._memo = {}

    def getParameterInfo(self):
        p_start = arcpy.Parameter(
            displayName="Startpunkt", name="start", datatype="GPFeatureRecordSetLayer",
            parameterType="Required", direction="Input")
        p_start.filter.list = ["Point"]
        p_start.value = _point_schema()

        p_end = arcpy.Parameter(
            displayName="Slutpunkt", name="end", datatype="GPFeatureRecordSetLayer",
            parameterType="Optional", direction="Input")
        p_end.filter.list = ["Point"]
        p_end.value = _point_schema()

        p_hours = arcpy.Parameter(
            displayName="Tidsram (timmar)", name="time_h", datatype="GPDouble",
            parameterType="Optional", direction="Input")

        p_visited = arcpy.Parameter(
            displayName="Redan besökta kommuner", name="visited", datatype="GPString",
            parameterType="Optional", direction="Input", multiValue=True)
        p_visited.filter.type = "ValueList"
        p_visited.filter.list = VISITED_LABELS

        p_table = arcpy.Parameter(
            displayName="Tabell med besökta kommuner", name="visited_table",
            datatype="GPTableView", parameterType="Optional", direction="Input")

        p_field = arcpy.Parameter(
            displayName="Fält med kommunkod eller kommunnamn", name="visited_field",
            datatype="GPString", parameterType="Optional", direction="Input")
        p_field.filter.type = "ValueList"
        p_field.filter.list = []
        p_field.enabled = False

        ws = _default_workspace()
        p_out_route = arcpy.Parameter(
            displayName="Utdata: rutt", name="out_route", datatype="DEFeatureClass",
            parameterType="Required", direction="Output")
        p_out_kom = arcpy.Parameter(
            displayName="Utdata: kommuner", name="out_kommuner", datatype="DEFeatureClass",
            parameterType="Required", direction="Output")
        if ws:
            p_out_route.value = os.path.join(ws, "Kommunrutt")
            p_out_kom.value = os.path.join(ws, "Kommunrutt_kommuner")

        p_gpx = arcpy.Parameter(
            displayName="Utdata: GPX-fil (valfri)", name="out_gpx", datatype="DEFile",
            parameterType="Optional", direction="Output")
        p_gpx.filter.list = ["gpx"]

        p_service = arcpy.Parameter(
            displayName="Uppehåll per kommun (min)", name="service_min", datatype="GPDouble",
            parameterType="Optional", direction="Input", category="Alternativ")
        p_service.value = 0
        p_speed = arcpy.Parameter(
            displayName="Hastighet jämfört med modellen (%)", name="speed_pct",
            datatype="GPDouble", parameterType="Optional", direction="Input",
            category="Alternativ")
        p_speed.value = 100
        p_solver = arcpy.Parameter(
            displayName="Beräkningstid för optimeringen (s)", name="solver_s",
            datatype="GPLong", parameterType="Optional", direction="Input",
            category="Alternativ")
        p_solver.value = DEFAULT_SOLVER_SECONDS

        p_osrm = arcpy.Parameter(
            displayName="Routingtjänst (OSRM)", name="osrm_url", datatype="GPString",
            parameterType="Optional", direction="Input", category="Datakällor")
        p_osrm.value = OSRM_URL
        p_layer = arcpy.Parameter(
            displayName="Kommungränser", name="kommun_layer", datatype="GPFeatureLayer",
            parameterType="Optional", direction="Input", category="Datakällor")
        p_layer.filter.list = ["Polygon"]
        p_lfield = arcpy.Parameter(
            displayName="Fält med kommunkod eller kommunnamn i kommungränser",
            name="kommun_field", datatype="GPString", parameterType="Optional",
            direction="Input", category="Datakällor")
        p_lfield.filter.type = "ValueList"
        p_lfield.filter.list = []
        p_lfield.enabled = False

        return [p_start, p_end, p_hours, p_visited, p_table, p_field, p_out_route, p_out_kom,
                p_gpx, p_service, p_speed, p_solver, p_osrm, p_layer, p_lfield]

    def isLicensed(self):
        return True

    def _sync_field(self, src, fld, memo_key):
        """Fyll i fältlistan när tabellen eller lagret byts. Snabbt: görs bara vid ändring."""
        text = src.valueAsText
        fld.enabled = bool(text)
        if not text:
            self._memo.pop(memo_key, None)
            return
        if self._memo.get(memo_key) == text:
            return
        self._memo[memo_key] = text
        fields, guess = _field_choices(text)
        fld.filter.list = fields
        if fld.value not in fields:
            fld.value = guess

    def updateParameters(self, parameters):
        p = {q.name: q for q in parameters}
        self._sync_field(p["visited_table"], p["visited_field"], "table")
        self._sync_field(p["kommun_layer"], p["kommun_field"], "layer")

    def updateMessages(self, parameters):
        p = {q.name: q for q in parameters}

        v = p["start"].value
        if v is not None and not _is_layer(v) and _has_features(v) is False:
            # Tom punktmall. Ett valt lager kontrolleras först vid körningen.
            p["start"].setErrorMessage("Välj ett lager eller rita en startpunkt i kartan.")

        h = p["time_h"].value
        if h is not None and not (0.25 <= h <= 96):
            p["time_h"].setErrorMessage("Tidsramen ska vara mellan 0,25 och 96 timmar.")

        s = p["speed_pct"].value
        if s is not None and not (50 <= s <= 150):
            p["speed_pct"].setErrorMessage("Ange mellan 50 och 150 procent.")
        m = p["service_min"].value
        if m is not None and not (0 <= m <= 600):
            p["service_min"].setErrorMessage("Ange mellan 0 och 600 minuter.")
        t = p["solver_s"].value
        if t is not None and not (1 <= t <= 3600):
            p["solver_s"].setErrorMessage("Ange mellan 1 och 3 600 sekunder.")

        if p["visited_table"].valueAsText and not p["visited_field"].valueAsText:
            p["visited_field"].setErrorMessage("Välj fältet med kommunkod eller kommunnamn.")
        if p["kommun_layer"].valueAsText and not p["kommun_field"].valueAsText:
            p["kommun_field"].setErrorMessage("Välj fältet med kommunkod eller kommunnamn.")

        url = (p["osrm_url"].valueAsText or "").strip()
        if url and not url.lower().startswith(("http://", "https://")):
            p["osrm_url"].setErrorMessage("Adressen ska börja med http:// eller https://.")
        elif url.rstrip("/") == OSRM_URL:
            p["osrm_url"].setWarningMessage(
                "Publik demoserver: till för måttlig användning, utan garanti för drift.")

        cache = (p["out_route"].valueAsText or "").lower()
        if cache and any(hint in cache for hint in _SYNC_HINTS):
            p["out_route"].setWarningMessage("Utdata ligger i en synkad mapp.")

    def execute(self, parameters, messages):
        p = {q.name: q for q in parameters}
        error = None
        try:
            start_pt = _read_point(p["start"].value, "Startpunkten", messages)
            end_pt = None
            if p["end"].valueAsText and _has_features(p["end"].value):
                end_pt = _read_point(p["end"].value, "Slutpunkten", messages)

            visited = set()
            unknown = []
            for label in (p["visited"].values or []):
                code = _resolve_kommun(label)
                (visited.add(code) if code else unknown.append(str(label)))
            if p["visited_table"].valueAsText:
                field = p["visited_field"].valueAsText
                if not field:
                    raise ValueError("Välj fältet med kommunkod eller kommunnamn i tabellen.")
                with arcpy.da.SearchCursor(p["visited_table"].valueAsText, [field]) as cur:
                    for (value,) in cur:
                        if value is None or str(value).strip() == "":
                            continue
                        code = _resolve_kommun(value)
                        (visited.add(code) if code else unknown.append(str(value)))
            if unknown:
                messages.addWarningMessage(
                    "{} värden i listan eller tabellen känns inte igen som kommuner och "
                    "hoppas över: {}".format(len(unknown), ", ".join(unknown[:10])
                                           + (" ..." if len(unknown) > 10 else "")))

            layer = p["kommun_layer"].valueAsText and p["kommun_layer"].value
            if layer and not p["kommun_field"].valueAsText:
                raise ValueError("Välj fältet med kommunkod eller kommunnamn i kommungränserna.")
            _run(start_pt, end_pt,
                 None if p["time_h"].value is None else float(p["time_h"].value), visited,
                 p["osrm_url"].valueAsText, layer or None, p["kommun_field"].valueAsText,
                 float(p["service_min"].value or 0), float(p["speed_pct"].value or 100),
                 int(p["solver_s"].value or DEFAULT_SOLVER_SECONDS),
                 p["out_route"].valueAsText, p["out_kommuner"].valueAsText, messages,
                 p["out_gpx"].valueAsText)
        except ValueError as exc:
            error = str(exc)
        finally:
            arcpy.ResetProgressor()
        # Utanför except-blocket, så att Pro inte skriver ut hela kedjan av undantag.
        if error:
            messages.addErrorMessage(error)
            raise arcpy.ExecuteError

    def postExecute(self, parameters):
        return
