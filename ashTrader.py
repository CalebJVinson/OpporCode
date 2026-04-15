from datamodel import TradingState, Order
from dataclasses import dataclass, field
from collections import deque
import json
import math


positionLimit = 80

ashQuoteSize = 8
ashBaseHalfSpread = 2
ashFallbackValue = 10000.0

rollingWindow = 120
centerBlend = 0.3

zWindow = 40
mildZ = 0.75
strongZ = 1.5


@dataclass
class AshState:
    mids: deque = field(default_factory=lambda: deque(maxlen=rollingWindow))
    fairValues: deque = field(default_factory=lambda: deque(maxlen=rollingWindow))
    lastFairValue: float = 0.0
    quoteCenter: float = ashFallbackValue

    def observe(self, mid, microprice, bidPresent, askPresent):
        if mid > 0:
            self.mids.append(mid)

        if bidPresent and askPresent and microprice > 0:
            self.lastFairValue = microprice
        elif mid > 0:
            self.lastFairValue = mid

        currentFairValue = self.fairValue(ashFallbackValue)
        self.fairValues.append(currentFairValue)

        if self.quoteCenter <= 0:
            self.quoteCenter = currentFairValue
        else:
            self.quoteCenter = (1.0 - centerBlend) * self.quoteCenter + centerBlend * currentFairValue

    def fairValue(self, fallback):
        if self.lastFairValue > 0:
            return self.lastFairValue
        return fallback

    def currentQuoteCenter(self, fallback):
        if self.quoteCenter > 0:
            return self.quoteCenter
        return self.fairValue(fallback)

    def zScore(self):
        if len(self.fairValues) < zWindow:
            return 0.0

        recent = list(self.fairValues)[-zWindow:]
        mean = sum(recent) / len(recent)
        variance = sum((value - mean) ** 2 for value in recent) / len(recent)
        stdev = math.sqrt(variance)

        if stdev < 1e-6:
            return 0.0

        currentValue = recent[-1]
        return (currentValue - mean) / stdev

    def toDict(self):
        return {
            "mids": list(self.mids),
            "fairValues": list(self.fairValues),
            "lastFairValue": self.lastFairValue,
            "quoteCenter": self.quoteCenter,
        }

    @classmethod
    def fromDict(cls, data):
        state = cls()
        state.mids = deque(data.get("mids", []), maxlen=rollingWindow)
        state.fairValues = deque(data.get("fairValues", []), maxlen=rollingWindow)
        state.lastFairValue = float(data.get("lastFairValue", 0.0))
        state.quoteCenter = float(data.get("quoteCenter", ashFallbackValue))
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

    fairValue = ashState.currentQuoteCenter(ashFallbackValue)
    zScore = ashState.zScore()

    bidHalfSpread = ashBaseHalfSpread
    askHalfSpread = ashBaseHalfSpread
    bidSize = ashQuoteSize
    askSize = ashQuoteSize

    # Mean-reversion posture:
    # high positive z => price rich => favor selling
    # high negative z => price cheap => favor buying
    if zScore >= strongZ:
        bidHalfSpread = ashBaseHalfSpread + 1
        askHalfSpread = max(1, ashBaseHalfSpread - 1)
        bidSize = max(1, ashQuoteSize - 2)
        askSize = ashQuoteSize + 2
    elif zScore >= mildZ:
        bidHalfSpread = ashBaseHalfSpread + 1
        askHalfSpread = ashBaseHalfSpread
        bidSize = max(1, ashQuoteSize - 1)
        askSize = ashQuoteSize + 1
    elif zScore <= -strongZ:
        bidHalfSpread = max(1, ashBaseHalfSpread - 1)
        askHalfSpread = ashBaseHalfSpread + 1
        bidSize = ashQuoteSize + 2
        askSize = max(1, ashQuoteSize - 2)
    elif zScore <= -mildZ:
        bidHalfSpread = ashBaseHalfSpread
        askHalfSpread = ashBaseHalfSpread + 1
        bidSize = ashQuoteSize + 1
        askSize = max(1, ashQuoteSize - 1)

    lean = inventoryLean(position)
    reservationPrice = fairValue - lean

    bidPrice = int(round(reservationPrice - bidHalfSpread))
    askPrice = max(bidPrice + 1, int(round(reservationPrice + askHalfSpread)))

    roomToBuy = max(0, positionLimit - position)
    roomToSell = max(0, positionLimit + position)

    orders = []

    if bidPresent and roomToBuy > 0:
        orders.append(Order(product, bidPrice, min(bidSize, roomToBuy)))

    if askPresent and roomToSell > 0:
        orders.append(Order(product, askPrice, -min(askSize, roomToSell)))

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