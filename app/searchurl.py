"""Sales Navigator people-search URLs, encoded the way LinkedIn writes them (and the way the team's
phantoms already receive them): filter text is URL-encoded inside the query, then the whole query is
encoded again with parentheses left bare."""
import re
import urllib.parse as up

BANDS = {  # Sales Nav COMPANY_HEADCOUNT ids
    "B": "1-10", "C": "11-50", "D": "51-200", "E": "201-500", "F": "501-1,000",
    "G": "1,001-5,000", "H": "5,001-10,000", "I": "10,001+",
}
BAND_ORDER = list(BANDS)


def _v(text, id_=None):
    t = up.quote(text, safe="")
    return f"(id:{id_},text:{t},selectionType:INCLUDED)" if id_ else f"(text:{t},selectionType:INCLUDED)"


def build(titles, counties, bands):
    """counties: [(geo_id, linkedin_text)]; bands: ['D','E',...]"""
    parts = ["(type:CURRENT_TITLE,values:List(" + ",".join(_v(t) for t in titles) + "))",
             "(type:REGION,values:List(" + ",".join(_v(txt, gid) for gid, txt in counties) + "))"]
    if bands:
        parts.append("(type:COMPANY_HEADCOUNT,values:List(" + ",".join(_v(BANDS[b], b) for b in bands) + "))")
    inner = "(filters:List(" + ",".join(parts) + "))"
    return "https://www.linkedin.com/sales/search/people?query=" + up.quote(inner, safe="()")


def parse(url):
    """Inverse of build, for history imports: {'regions': [(id,text)], 'bands': [...], 'titles': [...]}"""
    u = url
    for _ in range(3):
        u = up.unquote(u)

    def part(t):
        m = re.search(r"type:" + t + r",values:List\((.*?)\)\)(?:\)|,\(type)", u)
        return m.group(1) if m else ""

    regions = re.findall(r"id:(\d+),text:(.*?),selectionType", part("REGION"))
    bands = re.findall(r"id:([A-I]),text:", part("COMPANY_HEADCOUNT"))
    titles = re.findall(r"text:(.*?),selectionType", part("CURRENT_TITLE"))
    return {"regions": regions, "bands": bands, "titles": titles}
