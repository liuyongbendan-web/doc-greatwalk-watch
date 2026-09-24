"""DOC Great Walk 线路数据 —— 2026-08-23 从官方接口实测抓取。

placeId 用于 greatwalkplacefacility 接口；element_id 是网页下拉框的 DOM id（book.py 用）。
huts 是该线路全部住宿点（含 Campsite）。default_itinerary 是单向线路的标准连住顺序，
第 N 项 = 出发日 + N 晚；其余线路留空，用「任意空位」模式。
"""

TRACKS = {
    'Abel Tasman Coast Track': {
        "place_id": 875,
        "element_id": 'great-walk-1',
        "default_itinerary": [],
        "huts": [
            'Akersten Bay Campsite',
            'Anapai Bay Campsite',
            'Anchorage Campsite',
            'Anchorage Hut',
            'Apple Tree Bay Campsite',
            'Awaroa Campsite',
            'Awaroa Hut',
            'Bark Bay Campsite',
            'Bark Bay Hut',
            'Coquille Bay Campsite',
            'Mosquito Bay Campsite (private boat access only)',
            'Mutton Cove Campsite',
            'Observation Beach Campsite',
            'Onetahuti Bay Campsite',
            'Te Pukatea Bay Campsite',
            'Tinline Campsite',
            'Torrent Bay Village Campsite',
            'Totaranui Great Walk Campsite',
            'Waiharakeke Bay Campsite',
            'Watering Cove Campsite',
            'Whariwharangi Bay Campsite',
            'Whariwharangi Hut',
        ],
    },
    'Heaphy Track': {
        "place_id": 876,
        "element_id": 'great-walk-2',
        "default_itinerary": [],
        "huts": [
            'Aorere Campsite',
            'Brown Campsite',
            'Brown Hut',
            'Gouland Downs Campsite',
            'Gouland Downs Hut',
            'Heaphy Campsite',
            'Heaphy Hut',
            'James Mackay Campsite',
            'James Mackay Hut',
            'Katipo Creek Shelter Campsite',
            'Perry Saddle Campsite',
            'Saxon Campsite',
            'Saxon Hut',
            'Scotts Beach Campsite',
            'Perry Saddle Hut',
        ],
    },
    'Kepler Track': {
        "place_id": 872,
        "element_id": 'great-walk-3',
        "default_itinerary": ['Luxmore Hut', 'Iris Burn Hut', 'Moturau Hut'],
        "huts": [
            'Brod Bay Campsite',
            'Iris Burn Campsite',
            'Iris Burn Hut',
            'Luxmore Hut',
            'Moturau Hut',
        ],
    },
    'Lake Waikaremoana Track': {
        "place_id": 878,
        "element_id": 'great-walk-4',
        "default_itinerary": [],
        "huts": [
            'Korokoro Campsite',
            'Marauiti Hut',
            'Maraunui Campsite',
            'Panekire Hut',
            'Waiharuru Campsite',
            'Waiopaoa Hut',
            'Waiharuru Hut',
            'Waiopaoa Campsite',
        ],
    },
    'Milford Track': {
        "place_id": 873,
        "element_id": 'great-walk-5',
        "default_itinerary": ['Clinton Hut', 'Mintaro Hut', 'Dumpling Hut'],
        "huts": [
            'Clinton Hut',
            'Dumpling Hut',
            'Mintaro Hut',
        ],
    },
    'Paparoa Track': {
        "place_id": 880,
        "element_id": 'great-walk-6',
        "default_itinerary": ['Ces Clark Hut', 'Moonlight Tops Hut', 'Pororari Hut'],
        "huts": [
            'Ces Clark Hut',
            'Moonlight Tops Hut',
            'Pororari Hut',
        ],
    },
    'Rakiura Track': {
        "place_id": 877,
        "element_id": 'great-walk-7',
        "default_itinerary": [],
        "huts": [
            'Maori Beach Campsite',
            'North Arm Campsite',
            'North Arm Hut',
            'Port William Campsite',
            'Port William Hut',
        ],
    },
    'Routeburn Track': {
        "place_id": 874,
        "element_id": 'great-walk-8',
        "default_itinerary": ['Routeburn Flats Hut', 'Lake Mackenzie Hut'],
        "huts": [
            'Lake Mackenzie Campsite',
            'Lake Mackenzie Hut',
            'Routeburn Falls Hut',
            'Routeburn Flats Campsite',
            'Routeburn Flats Hut',
        ],
    },
    'Tongariro Northern Circuit': {
        "place_id": 879,
        "element_id": 'great-walk-9',
        "default_itinerary": [],
        "huts": [
            'Mangatepopo Campsite',
            'Mangatepopo Hut',
            'Oturere Campsite',
            'Oturere Hut',
            'Waihohonu Campsite',
            'Waihohonu Hut',
        ],
    },
    'Whanganui Journey': {
        "place_id": 881,
        "element_id": 'great-walk-10',
        "default_itinerary": [],
        "huts": [
            'John Coull Campsite',
            'John Coull Hut',
            'Maharanui Campsite',
            'Mangapapa Campsite',
            'Mangapurua Campsite',
            'Mangawaiiti Campsite',
            'Ngāporo Campsite',
            'Ōhauora Campsite',
            'Ōhinepane Campsite',
            'Poukaria Campsite',
            'Tīeke Campsite',
            'Tīeke Kāinga Hut',
            'Whakahoro Bunkroom',
            'Whakahoro Campsite',
        ],
    },
}


def get(name):
    """按名字取线路，支持不区分大小写的模糊匹配（"milford" -> "Milford Track"）"""
    if name in TRACKS:
        return TRACKS[name]
    low = name.strip().lower()
    hits = [k for k in TRACKS if low in k.lower()]
    if len(hits) == 1:
        return TRACKS[hits[0]]
    raise KeyError(f"未知线路 {name!r}，可选: {list(TRACKS)}")


def resolve_name(name):
    """把模糊名字规范成正式名字"""
    if name in TRACKS:
        return name
    low = name.strip().lower()
    hits = [k for k in TRACKS if low in k.lower()]
    if len(hits) == 1:
        return hits[0]
    raise KeyError(f"未知线路 {name!r}，可选: {list(TRACKS)}")
