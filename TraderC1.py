from datamodel import TradingState, Order
from dataclasses import dataclass, field
from collections import deque
import json
import math


positionLimit = 80
rollingWindow = 500
shortWindow = 50
maxDelta = 15
maxSkewTicks = 4


@dataclass
class RollingState:
    mids: deque = field(default_factory=lambda: deque(maxlen=rollingWindow))
    returns: deque = field(default_factory=lambda: deque(maxlen=rollingWindow))
    returnsShort: deque = field(default_factory=lambda: deque(maxlen=shortWindow))
    deltaCounts: dict = field(default_factory=dict)

    tickWeight: float = 0.0
    totalTicks: int = 0
    volShockTicks: int = 0

    lastMid: float = 0.0
    lastFairValue: float = 0.0

    def observe(self, mid, microprice, bidPresent, askPresent, tradePrices):
        self.totalTicks += 1
        self.tickWeight += 1.0

        if self.lastMid > 0 and mid > 0:
            change = mid - self.lastMid
            self.returns.append(change)
            self.returnsShort.append(change)

        if mid > 0:
            self.lastMid = mid
            self.mids.append(mid)

        if bidPresent and askPresent and microprice > 0:
            self.lastFairValue = microprice

        anchor = self.fairValue(mid)
        if anchor > 0:
            for price in tradePrices:
                distance = int(round(abs(price - anchor)))
                if 0 <= distance <= maxDelta:
                    self.deltaCounts[distance] = self.deltaCounts.get(distance, 0.0) + 1.0

        if self.totalTicks % 500 == 0:
            self.tickWeight *= 0.5
            for distance in list(self.deltaCounts.keys()):
                self.deltaCounts[distance] *= 0.5

    def sigma(self):
        if len(self.returns) < 20:
            return 2.0
        return standardDeviation(self.returns)

    def sigmaShort(self):
        if len(self.returnsShort) < 20:
            return self.sigma()
        return standardDeviation(self.returnsShort)

    def updateVolShock(self):
        longSigma = self.sigma()
        if longSigma < 0.1:
            self.volShockTicks = 0
            return

        ratio = self.sigmaShort() / longSigma
        if ratio > 1.5:
            self.volShockTicks = min(self.volShockTicks + 1, 5)
        else:
            self.volShockTicks = max(self.volShockTicks - 1, 0)

    def volMultiplier(self):
        return 1.5 if self.volShockTicks >= 3 else 1.0

    def tradeRate(self, distance):
        if self.tickWeight < 20:
            return 0.01 * math.exp(-0.2 * distance)
        return self.deltaCounts.get(distance, 0.0) / max(1.0, self.tickWeight)

    def fairValue(self, mid):
        if self.lastFairValue > 0:
            return self.lastFairValue
        if mid > 0:
            return mid
        return 0.0

    def shortDrift(self, lookback):
        if len(self.mids) < lookback:
            return 0.0
        recent = list(self.mids)[-lookback:]
        return recent[-1] - recent[0]

    def toDict(self):
        return {
            "mids": list(self.mids),
            "returns": list(self.returns),
            "returnsShort": list(self.returnsShort),
            "deltaCounts": self.deltaCounts,
            "tickWeight": self.tickWeight,
            "totalTicks": self.totalTicks,
            "volShockTicks": self.volShockTicks,
            "lastMid": self.lastMid,
            "lastFairValue": self.lastFairValue,
        }

    @classmethod
    def fromDict(cls, data):
        state = cls()
        state.mids = deque(data.get("mids", []), maxlen=rollingWindow)
        state.returns = deque(data.get("returns", []), maxlen=rollingWindow)
        state.returnsShort = deque(data.get("returnsShort", []), maxlen=shortWindow)
        state.deltaCounts = {int(k): float(v) for k, v in data.get("deltaCounts", {}).items()}
        state.tickWeight = float(data.get("tickWeight", data.get("totalTicks", 0.0)))
        state.totalTicks = int(data.get("totalTicks", 0))
        state.volShockTicks = int(data.get("volShockTicks", 0))
        state.lastMid = float(data.get("lastMid", 0.0))
        state.lastFairValue = float(data.get("lastFairValue", 0.0))
        return state


def standardDeviation(values):
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((value - mean) ** 2 for value in values) / n
    return math.sqrt(variance)


def chooseHalfSpread(state):
    bestDistance = 1
    bestScore = -1.0

    for distance in range(1, maxDelta + 1):
        score = state.tradeRate(distance) * distance
        if score > bestScore:
            bestScore = score
            bestDistance = distance

    return bestDistance


def inventorySkew(position, state, horizon):
    if position == 0:
        return 0.0

    halfSpread = chooseHalfSpread(state)
    fillRate = max(state.tradeRate(halfSpread), 1e-3)
    rawSkew = position * state.sigma() * math.sqrt(1.0 / (fillRate * horizon))

    return max(-maxSkewTicks, min(maxSkewTicks, rawSkew))


class Strategy:
    symbol = ""
    horizon = 3000

    def quoteSize(self):
        return 10

    def fairValueShift(self, state):
        return 0.0

    def extraSpread(self, state):
        return 0

    def allowOneSidedQuote(self):
        return True

    def decide(self, state, mid, position, bidPresent, askPresent):
        fairValue = state.fairValue(mid)
        if fairValue <= 0:
            return []

        if (bidPresent != askPresent) and not self.allowOneSidedQuote():
            return []

        fairValue += self.fairValueShift(state)

        halfSpread = chooseHalfSpread(state)
        halfSpread += self.extraSpread(state)

        skew = inventorySkew(position, state, self.horizon)

        volMult = state.volMultiplier()
        halfSpread = max(1, int(round(halfSpread * volMult)))
        sizeCap = max(1, int(round(self.quoteSize() / volMult)))

        reservationPrice = fairValue - skew

        bidPrice = min(int(round(reservationPrice - halfSpread)), int(fairValue))
        askPrice = max(int(round(reservationPrice + halfSpread)), int(fairValue) + 1)

        roomToBuy = max(0, positionLimit - position)
        roomToSell = max(0, positionLimit + position)

        orders = []

        if bidPresent and roomToBuy > 0:
            buySize = min(sizeCap, roomToBuy)
            if buySize > 0:
                orders.append((bidPrice, buySize))

        if askPresent and roomToSell > 0:
            sellSize = min(sizeCap, roomToSell)
            if sellSize > 0:
                orders.append((askPrice, -sellSize))

        return orders


class AshStrategy(Strategy):
    symbol = "ASH_COATED_OSMIUM"
    horizon = 3000

    def quoteSize(self):
        return 10

    def allowOneSidedQuote(self):
        return True


class PepperStrategy(Strategy):
    symbol = "INTARIAN_PEPPER_ROOT"
    horizon = 1000

    def quoteSize(self):
        return 6

    def fairValueShift(self, state):
        drift = state.shortDrift(20)
        if drift > 2:
            return 1.0
        if drift < -2:
            return -1.0
        return 0.0

    def extraSpread(self, state):
        drift = abs(state.shortDrift(20))
        if drift > 4:
            return 1
        return 0

    def allowOneSidedQuote(self):
        return False


strategies = {
    AshStrategy.symbol: AshStrategy(),
    PepperStrategy.symbol: PepperStrategy(),
}


class Trader:
    def run(self, state: TradingState):
        try:
            stateBlob = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            stateBlob = {}

        result = {}

        for product, depth in state.order_depths.items():
            strategy = strategies.get(product)
            if strategy is None:
                continue

            rollingState = RollingState.fromDict(stateBlob[product]) if product in stateBlob else RollingState()

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
                if totalVolume > 0:
                    microprice = (bestBid * askVolume + bestAsk * bidVolume) / totalVolume
                else:
                    microprice = mid
            else:
                bestBid = max(buys.keys()) if bidPresent else 0.0
                bestAsk = min(sells.keys()) if askPresent else 0.0

                if bidPresent:
                    mid = bestBid
                elif askPresent:
                    mid = bestAsk
                else:
                    mid = rollingState.lastFairValue or 0.0

                microprice = rollingState.lastFairValue or mid

            tradePrices = [trade.price for trade in state.market_trades.get(product, []) or []]

            rollingState.observe(mid, microprice, bidPresent, askPresent, tradePrices)
            rollingState.updateVolShock()

            position = state.position.get(product, 0)
            quotes = strategy.decide(rollingState, mid, position, bidPresent, askPresent)

            result[product] = [
                Order(product, int(price), int(quantity))
                for price, quantity in quotes
            ]

            stateBlob[product] = rollingState.toDict()

        return result, 0, json.dumps(stateBlob)