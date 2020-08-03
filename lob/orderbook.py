import os
from pathlib import Path
from collections import deque
import lmdb
from time import time
import stats

from .orderlist import OrderList
from .model import Quote, Trade, decode

TS = stats.Stats()

FLUSH_TIME  = 1      # Number of seconds until flush()
FLUSH_COUNT = 40000  # Number of orders until flush()

class OrderBook(object):
    def __init__(self, env, trades_dir):
        self.tape = deque(maxlen=None) # Index [0] is most recent trade
        self.trades_dir = trades_dir

        self.verbose = False

        # LMDB
        self.env = env

        self.bids = OrderList(self.env, 'bid')
        self.asks = OrderList(self.env, 'ask')

        # Since last flush
        self.flushed = time()
        self.count = 0

    # Nanoseconds µs
    def time_ns(self):
        return int(time() * 1000 * 1000)

    @TS.timeit
    def check_flush(self):
        elapsed = time() - self.flushed
        if (self.count > FLUSH_COUNT or elapsed > FLUSH_TIME):
            self.flush()
            self.flushed = time()
            self.count = 0

    @TS.timeit
    def flush(self):
        elapsed = time() - self.flushed
        out = '%-10s: orders: %-8d trades: %-8d time: %.2fs orders/sec:%-8d' % (
            'flush', self.count, len(self.tape), elapsed,
            self.count / elapsed
        )
        print(out)

        with self.env.begin(write=True) as txn:
            self.bids.flush(txn)
            self.asks.flush(txn)
            self.flush_trades()
            #print('sleep 5 seconds after flush()..')
            #time.sleep(5)
            # write out trades
            # write out order update logs (status and qty change)
            # write out book cache (for charts)

            # I think subsequent trade processing can do these:
            # write out ledgers (do this here?)
            # write out ohlcv? (can trades produce this?)

    @TS.timeit
    def flush_trades(self):
        if not self.tape:
            return
        if not os.path.exists(self.trades_dir):
            os.mkdir(self.trades_dir)
        tmpfile = Path(self.trades_dir) / '.tmp'
        permfile = Path(self.trades_dir) / str(self.time_ns())
        with open(tmpfile, 'w') as f:
            for t in self.tape:
                a = [str(t[x]) for x in ('time','price','qty','maker','taker')]
                #out = "%s,%s,%s\n" % (t['time'], t['price'], t['qty'])
                out = ",".join(a) + "\n"
                # maker id,side
                # taker id,side
                f.write(out)

        os.rename(tmpfile, permfile)
        self.tape = deque(maxlen=None)

    @TS.timeit
    def processOrder(self, quote):
        orderInBook = None
        self.count += 1
        if quote.type == 'market':
            trades = self.processMarketOrder(quote)
        elif quote.type == 'limit':
            trades, orderInBook = self.processLimitOrder(quote)
        else:
            sys.exit("processOrder() given neither 'market' nor 'limit'")

        return trades, orderInBook

    def processMarketOrder(self, quote):
        trades = []
        qtyToTrade = quote.qty
        if quote.side == 'bid':
            olist = self.asks
        elif quote.side == 'ask':
            olist = self.bids
        qtyToTrade, newTrades = self.processList(olist, quote, qtyToTrade)
        trades += newTrades
        return trades

    def processLimitOrder(self, quote):
        orderInBook = None
        trades = []

        qtyToTrade = quote.qty
        # Other side
        if quote.side == 'bid':
            olist = self.asks
        elif quote.side == 'ask':
            olist = self.bids
        qtyToTrade, newTrades = self.processList(olist, quote, qtyToTrade)
        trades += newTrades

        # If volume remains, add to book
        if qtyToTrade > 0:
            quote.qty = qtyToTrade
            # This side
            if quote.side == 'bid':
                tlist = self.bids
            elif quote.side == 'ask':
                tlist = self.asks
            tlist.insert(quote)
            orderInBook = quote

            # Book Cache Transaction
            #booktx = [quote.side, quote.price, quote.qty]

        return trades, orderInBook

    def processList(self, olist, quote, qtyAss):
        qtyToTrade = qtyAss
        trades = []
        #print('processList', '-'*50)
        cnt = 0
        is_limit = quote.type == 'limit'

        for i, seq_key in enumerate(olist):
            o = olist.get_order(seq_key)
            if qtyToTrade <= 0:
                break
            if is_limit and olist.side == 'ask' and o.price > quote.price:
                break
            elif is_limit and olist.side == 'bid' and o.price < quote.price:
                break

            #foo = '  %-4d %s' % (i,o)
            #foo = ','.join((str(o.price),str(o.id)))
            #foo = "%d,%d" % (o.price,o.id)

            cnt += 1
            #print(cnt, o)
            tradedPrice = o.price
            counterparty = o.id
            if qtyToTrade < o.qty:
                tradedQty = qtyToTrade
                # Amend book order
                newBookQty = o.qty - qtyToTrade
                olist.update_qty(o, newBookQty)
                qtyToTrade = 0
            elif qtyToTrade == o.qty:
                tradedQty = qtyToTrade
                olist.delete(o)
                qtyToTrade = 0
            else:
                tradedQty = o.qty
                olist.delete(o)
                # We need to keep eating into volume at this price
                qtyToTrade -= tradedQty

            if self.verbose:
                print('TRADE qty:%d @ $%.2f   p1=%d p2=%d  (left:%d)' % (
                    tradedQty, tradedPrice,
                    counterparty, quote.id, qtyToTrade
                ))

            # Book Cache Transaction
            #booktx = [olist.side, o.price, tradedQty * -1]

            # Trade Transaction
            tx = {
                'time'  : self.time_ns(),
                'price' : tradedPrice,
                'qty'   : tradedQty,
                # maker is order, taker is quote
                #'maker': [olist.side, o.id],
                #'taker': [quote.side, quote.id],
                'maker': o.id,
                'taker': quote.id,
            }

            self.tape.append(tx)
            trades.append(tx)

        olist.apply_deletes()
        return qtyToTrade, trades

    def dump_book(self):
        s1 = time()
        self.bids.dump_book()
        self.asks.dump_book()
        print("%.2f ms elapsed." % ((time() - s1) * 1000,))

    def __str__(self):
        return str(self)
