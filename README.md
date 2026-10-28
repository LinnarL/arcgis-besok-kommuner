# Besök kommuner

ArcGIS Pro Python toolbox som planerar den körbara rutt mellan två punkter som går in i flest nya kommuner i Sverige inom en given tid. Kommuner du redan har besökt kan anges och räknas då inte.

## Krav

- ArcGIS Pro 3.x (testad på 3.6). Ingen extra licens eller extension behövs.
- Internetanslutning. Inga paket utöver `arcpy` och `numpy`.

## Lägga till i Pro

Catalog-fönstret: högerklicka Toolboxes, Add Toolbox, välj `BesokKommuner.pyt`. Verktyget heter "Planera rutt genom flest kommuner".

## Parametrar

| Parameter | Beskrivning |
|---|---|
| Startpunkt | Punktlager eller en ritad punkt. Första punkten används. |
| Slutpunkt | Valfri. Tom betyder att bara startpunkten används: rutten börjar och slutar där och utnyttjar hela tidsramen (rundtur). |
| Tidsram (timmar) | Total tid från start till slut, körtid och uppehåll inräknat. 0,25 till 96. |
| Redan besökta kommuner | Kryssruteslista med alla 290 kommuner. |
| Tabell med besökta kommuner | Valfri tabell. Läggs till kryssrutornas val. |
| Fält med kommunkod eller kommunnamn | Fältet i tabellen. Kod (`0180`) eller namn (`Stockholm`) går bra. |
| Utdata: rutt | Linjeklass med rutten och dess tid, längd och antal nya kommuner. |
| Utdata: kommuner | Polygonklass med status per kommun, stoppnummer och ankomsttid. |
| Utdata: GPX-fil | Valfri. En rutt (start, stopp, slut) och ett spår med hela vägen, för GPS eller mobilapp. |
| Uppehåll per kommun (min) | Standard 0. Tid som räknas av per kommun du stannar i. |
| Hastighet jämfört med modellen (%) | Standard 100. 90 ger 11 procent längre körtid. |
| Beräkningstid för optimeringen (s) | Standard 30. |
| Routingtjänst (OSRM) | Standard är den publika demoservern. |
| Kommungränser, fält | Valfritt eget polygonlager. Lagrets urval och definitionsfråga gäller. |

Status i kommunlagret: `Nytt stopp`, `Nytt, passeras`, `Nytt, start/slut`, `Redan besökt`, `Inte besökt`.

## Hur det fungerar

- Restiderna kommer från OSRM (bilprofil, OpenStreetMap). Tiden följer vägarnas skyltade hastighet och vägtyp, så en motorväg är snabbare än en landsväg. Färjor ingår med den tid OpenStreetMap anger, utan väntetid.
- Varje kommun representeras av en punkt inuti polygonen. Restiderna mellan de 290 punkterna hämtas en gång (16 anrop) och sparas i `%LOCALAPPDATA%\BesokKommuner`. Senare körningar hämtar bara tiderna till och från start- och slutpunkten.
- Rutten väljs med en heuristik (billigaste insättning, 2-opt, flytt av stopp och iterativ lokal sökning) som ger flest nya kommuner inom tidsramen. Heuristiken garanterar inte det globala optimumet, men tre olika slumptal gav samma rutt i test.
- Den verkliga rutten hämtas sedan från OSRM och skärs mot kommunerna. En kommun räknas som passerad om rutten är minst 500 m inne i den, och räknas då som ny utan att vara ett stopp.
- Blir verklig tid längre än tidsramen planeras rutten om med marginal.

## Att känna till

- En kommun räknas som nådd när rutten går in i den. Stoppet ligger vid en punkt inuti kommunen, inte vid gränsen, så en rutt kan ibland nå fler kommuner på samma tid om den bara tangerar dem. Det som passeras på vägen räknas ändå.
- Standardgränserna är generaliserade (cirka 1 km) och kommer från `okfse/sweden-geojson`. Ange ett eget kommunlager för exakta gränser. Saknas en kommun i lagret, eller utesluts den av ett urval, kan den inte besökas.
- Publika OSRM-servern är till för måttlig användning och saknar driftgaranti. Kör en egen instans för regelbundet bruk och ange adressen i verktyget.
- Gotland och andra öar nås bara om det finns en färja i OpenStreetMap-data. Kommuner utan väg från start eller till slut hoppas över.
- Restiden är modellens, utan trafik, rast och väntetid. Använd hastighetsprocenten och uppehållstiden för att anpassa.
