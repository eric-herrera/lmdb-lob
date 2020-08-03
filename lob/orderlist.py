import stats
from sortedcontainers import SortedList, SortedSet
from lob.model import Order, encode, decode

TS = stats.Stats()

# This the number of orders that will be held in memory.
ORDERS_SIZE = 5000

class OrderList:
    def __init__(self, env, side):
        self.env = env
        self.side = side

        #self.idb = env.open_db(b'ids')

        if side == 'bid':
            self.db = env.open_db(b'bids')
        elif side == 'ask':
            self.db = env.open_db(b'asks')#, dupsort=True)
        else:
            raise Exception('Invalid side: '+side)

        """
        Sequence key

        The sequence key is used in both the orders SortedList and lmdb
        databases bids and asks. The key is 16 bytes. 8 bytes price +
        8 bytes id (sequence number). The id (sequence number) represents
        the order of orders.

        Processing order is as follows:
        For asks, ascending price and ascending id. The first order to match
        is the lowest price and the first order at that price.

        For bids, descending price and ascending id. The first order to match
        is the highest price and the first order at that price.

        Price + id for asks are already in combined sort order. For bids,
        we change the price to a negative number so that sorting both combined
        result in proper order. Both sides are iterated correctly using next().

        """

        # Current batch of orders
        self.order_idx = {}          # order.id -> Order
        self.orders = SortedList()   # [sequence key..]

        # Current pending operations
        self.pending = {}            # order.id -> [ops..]

        # Orders waiting to be deleted are moved here
        self.deleted_order_idx = {}  # order.id -> Order

        self.iter_deletes = []
        self.iter_idx = 0

        # Hydrate current orders
        self.refill()

        # Debug
        #print(self.side)
        #print('orders:',len(self.orders))
        #print('order_idx:',len(self.order_idx.keys()))


    def __iter__(self):
        if len(self.iter_deletes):
            raise Exception('Deletes must be applied before iterating again.')

        self.iter_idx = 0
        return self

    def __next__(self):
        idx = self.iter_idx

        # The current position is beyond orders. Attempt to refill
        if idx > len(self.orders) - 1:
            self.refill()

        if len(self.orders) == 0 or idx > len(self.orders) - 1:
            raise StopIteration

        self.iter_idx += 1
        return self.orders[idx]

    def __len__(self):
        return len(self.orders)

    def seq_key(self, order):
        price = order.price
        if self.side == 'bid':
            price = order.price * -1
        return encode(price) + encode(order.id)

    def update_qty(self, order, qty):
        self.order_idx[order.id].qty = qty
        self.add_pending(order, 'qty')

    @TS.timeit
    def insert(self, quote):
        order = Order(quote.to_dict())
        seq_key = self.seq_key(order)

        # Special rule - The thing that separates memory list vs db is the
        # last order (self.orders[-1]. This last order is used to extend the
        # memory list. If this value isn't in the db, we have no way to know
        # which records we need. This condition only occurs when the book
        # is empty. Once this first insert is pushed to both memorylist
        # and db, all subsequent inserts can properly segregate.
        if len(self.orders) == 0:
            self.orders.add(seq_key)
            self.order_idx[order.id] = order

            with self.env.begin(write=True) as txn:
                self.db_insert(txn, order)
                order.in_db = True
        else:
            self.order_idx[order.id] = order
            self.add_pending(order, 'insert')
            # Orders within memory list range are added
            if (len(self.orders) > 0 and seq_key < self.orders[-1]):
                self.orders.add(seq_key)

            #print('insert() added:',order.id,'to orders:',added, ' total:',len(self.orders))

    @TS.timeit
    def update(self, orderUpdate):
        order = self.orderMap[orderUpdate['idNum']]
        originalVolume = order.qty
        if orderUpdate['price'] != order.price:
            # Price changed
            orderList = self.priceMap[order.price]
            orderList.removeOrder(order)
            if len(orderList) == 0:
                self.removePrice(order.price)
            self.insertOrder(orderUpdate)
        else:
            # Quantity changed
            order.updateQty(orderUpdate['qty'], orderUpdate['timestamp'])
        self.volume += order.qty-originalVolume

    @TS.timeit
    def delete(self, order):
        self.iter_deletes.append(order)
        self.add_pending(order, 'remove')

    # During orders iteration inserts don't occur, only qty updates and deletes.
    # Deletes are applied after iteration
    def apply_deletes(self):
        while self.iter_deletes:
            o = self.iter_deletes.pop()
            seq_key = self.seq_key(o)
            del self.order_idx[o.id]
            self.deleted_order_idx[o.id] = o
            self.orders.remove(seq_key)

    @TS.timeit
    def refill(self):
        end_order = None
        order_id = None
        if len(self.orders) > 0:
            seq_key = self.orders[-1]
            order_id = decode(seq_key[8:])
            end_order = self.order_idx[order_id]

        #print('refill() side:',self.side,' end_order:',order_id,'idx:',self.iter_idx)
        orders, order_idx = self.db_get_list(order=end_order)
        self.orders += orders
        self.order_idx.update(order_idx)
        #print('refill() added',len(orders),'orders. total:',len(self.orders))

    def get_order(self, seq_key):
        order_id = decode(seq_key[8:])
        return self.order_idx[order_id]

    @TS.timeit
    def db_insert(self, txn, o):
        seq_key = self.seq_key(o)
        value = encode(o.qty) + encode(o.account_id)
        r1 = txn.put(seq_key, value, db=self.db)
        #r1 = txn.put(encode(o.id), encode(o.qty), db=self.idb)
        #r2 = txn.put(seq_key[:8], seq_key[8:], db=self.db)
        if not r1:
            raise Exception('Should we die on duplicate insert?')

    @TS.timeit
    def db_delete(self, txn, o):
        seq_key = self.seq_key(o)
        r1 = txn.delete(seq_key, db=self.db)
        #r1 = txn.delete(seq_key[:8], seq_key[8:], db=self.db)
        #r2 = txn.delete(encode(o.id), db=self.idb)
        if not r1:
            self.dump_pending()
            print('r1:',r1)
            print(o)
            raise Exception('Should we die on failed delete?')

    @TS.timeit
    def db_update(self, txn, o):
        seq_key = self.seq_key(o)
        value = encode(o.qty) + encode(o.account_id)
        r = txn.put(seq_key, value, db=self.db)
        #r = txn.put(encode(o.id), encode(o.qty), db=self.idb)

    @TS.timeit
    def db_get_list(self, order=None, size=ORDERS_SIZE):
        orders = []
        order_idx = {}
        with self.env.begin(db=self.db) as txn:
            cur = txn.cursor()

            # If order is supplied, the cursor starts at the next one.
            seq_key = None
            if order:
                seq_key = self.seq_key(order)

            if seq_key:
                if not cur.set_key(seq_key):
                    print('id:',decode(seq_key[8:]))
                    print('price:',abs(decode(seq_key[:8])))
                    raise Exception('Failed to set_key')
                if not cur.next():
                    return orders, order_idx
            elif not cur.first():
                return orders, order_idx

            # Fetch list from db
            pairs = []
            for cnt, (k, v) in enumerate(cur.iternext(True, True)):
                # Break on size limit
                if size != -1 and cnt > size:
                    break
                pairs.append((k,v))

            for kv in pairs:
                seq_key, value = kv
                data = {
                    'id'        : decode(seq_key[8:]),
                    'price'     : abs(decode(seq_key[:8])),
                    'qty'       : decode(value[:8]),
                    'account_id': decode(value[8:]),
                    'in_db'     : True
                }
                o = Order(**data)
                orders.append(seq_key)
                order_idx[data['id']] = o

        return orders, order_idx

    def dump_book(self):
        with self.env.begin(db=self.db) as txn:
            cur = txn.cursor()
            cur.first()

            for cnt, (k, v) in enumerate(cur.iternext(True, True)):
                id_num     = decode(k[8:])
                price      = abs(decode(k[:8]))
                qty        = decode(v[:8])
                account_id = decode(v[8:])
                print("%s %10d %10d %10d %10d" % (
                    self.side, price, id_num, qty, account_id))

    def add_pending(self, order, state):
        if order.id not in self.pending:
            self.pending[order.id] = []
        self.pending[order.id].append(state)

    # Flush changes to disk
    @TS.timeit
    def flush(self, txn):
        #print('flush %3s orders:%8d' % (self.side, len(self.orders)))
        for order_id in self.pending.keys():
            ops = self.pending[order_id]
            if ops[-1] == 'remove':
                o = self.deleted_order_idx[order_id]
            else:
                o = self.order_idx[order_id]

            if ops[0] == 'insert' and ops[-1] == 'remove':
                pass
            elif ops[-1] == 'insert':
                self.db_insert(txn, o)
                o.in_db = True
            elif ops[-1] == 'remove' and o.in_db:
                self.db_delete(txn, o)
            elif ops[-1] == 'qty':
                self.db_update(txn, o)

        # After everything is flushed, trim to ORDERS_SIZE ?

        self.pending = {}
        self.deleted_order_idx = {}

    def dump_pending(self):
        print("------ Pending -------")
        print(self.side+":")
        for k in sorted(self.pending.keys()):
            v = self.pending[k]
            if k in self.deleted_order_idx:
                o = self.deleted_order_idx[k]
            else:
                o = self.order_idx[k]
            print(("%10d %s %s" % (k, v, o)))


