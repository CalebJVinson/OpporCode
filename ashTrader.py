from datamodel import TradingState, Order
from dataclasses import dataclass, field
from collections import deque
import json


positionLimit = 80

ashQuoteSize = 4
ashBaseHalfSpread = 2
ashFallbackValue = 10000.0

rollingWindow = 120


@dataclass
class AshState:
    mids: deque = field(default_factory=lambda: deque(maxlen=rollingWindow))
    lastFairValue: float = 0.0

    def observe(self, mid, microprice, bidPresent, askPresent):
        if mid > 0:
            self.mids.append(mid)

        if bidPresent and askPresent and microprice > 0:
            self.lastFairValue = microprice
        elif mid > 0:
            self.lastFairValue = mid

    def fairValue(self, fallback):
        if self.lastFairValue > 0:
            return self.lastFairValue
        return fallback

    def toDict(self):
        return {
            "mids": list(self.mids),
            "lastFairValue": self.lastFairValue,
        }

    @classmethod
    def fromDict(cls, data):
        state = cls()
        state.mids = deque(data.get("mids", []), maxlen=rollingWindow)
        state.lastFairValue = float(data.get("lastFairValue", 0.0))
        return state


def inventoryLean(position):
    if position > 40:
        return 2
    if position > 15:
        return 1
    if position < -40:
        return -2
    if position < -15:
        return -1
    return 0


def getBookStats(depth, fallbackFairValue):
    buys = depth.buy_orders or {}
    sells = depth.sell_orders or {}

    bidPresent = bool(buys)
    askPresent = bool(sells)

    if bidPresent and askPresent:
        bestBid = max(buys.keys())
        bestAsk = min(sells.keys())

        bidVolume = buys[bestBid]
        askVolume = abs(sells[bestAsk])

        mid = (bestBid + bestAsk) / 2.0
        totalVolume = bidVolume + askVolume
        microprice = (bestBid * askVolume + bestAsk * bidVolume) / totalVolume if totalVolume > 0 else mid
    else:
        bestBid = max(buys.keys()) if bidPresent else 0.0
        bestAsk = min(sells.keys()) if askPresent else 0.0

        if bidPresent:
            mid = bestBid
        elif askPresent:
            mid = bestAsk
        else:
            mid = fallbackFairValue

        microprice = mid

    return bidPresent, askPresent, mid, microprice


def makeAshOrders(product, depth, ashState, position):
    bidPresent, askPresent, mid, microprice = getBookStats(depth, ashState.fairValue(ashFallbackValue))
    ashState.observe(mid, microprice, bidPresent, askPresent)

    fairValue = ashState.fairValue(ashFallbackValue)
    lean = inventoryLean(position)
    reservationPrice = fairValue - lean

    bidPrice = int(round(reservationPrice - ashBaseHalfSpread))
    askPrice = max(bidPrice + 1, int(round(reservationPrice + ashBaseHalfSpread)))

    roomToBuy = max(0, positionLimit - position)
    roomToSell = max(0, positionLimit + position)

    orders = []

    if bidPresent and roomToBuy > 0:
        buySize = min(ashQuoteSize, roomToBuy)
        orders.append(Order(product, bidPrice, buySize))

    if askPresent and roomToSell > 0:
        sellSize = min(ashQuoteSize, roomToSell)
        orders.append(Order(product, askPrice, -sellSize))

    return orders


class Trader:
    def run(self, state: TradingState):
        try:
            stateBlob = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            stateBlob = {}

        result = {}

        for product, depth in state.order_depths.items():
            if product != "ASH_COATED_OSMIUM":
                continue

            ashState = AshState.fromDict(stateBlob[product]) if product in stateBlob else AshState()
            position = state.position.get(product, 0)

            result[product] = makeAshOrders(product, depth, ashState, position)
            stateBlob[product] = ashState.toDict()

        traderData = json.dumps(stateBlob)
        return result, 0, traderData