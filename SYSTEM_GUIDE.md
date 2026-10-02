# How the mildew monitoring system works

A plain-language guide to the whole system: what each part does, and what
happens to a weather reading from the moment it is measured in the vineyard
to the moment it can be seen in the app.

No technical background is needed. Terms in *italics* are explained in the
[glossary](#glossary) at the end.

## The system in one paragraph

Small battery-powered stations in the vineyard, called *leaf nodes*, measure
temperature and humidity every 5 minutes. Each one saves the reading on its
own memory card and sends it by long-range radio to a receiver, the *central
node*. The central node confirms that it got the reading and hands it to a
small computer, a Raspberry Pi, which stores it in a database and serves the
VitiSense app to phones over its own Wi-Fi. The app uses the readings to show
current conditions and warn when they favour powdery or downy mildew.

## The parts

```
   IN THE VINEYARD                      AT THE BASE
 ┌────────────────┐   radio    ┌────────────────┐  USB   ┌────────────────┐  Wi-Fi  ┌───────┐
 │   Leaf node    │ ─────────► │  Central node  │ ─────► │  Raspberry Pi  │ ──────► │ Phone │
 │ sensor + clock │ ◄───────── │ radio receiver │  cable │ database + app │         │  app  │
 │ + memory card  │  "got it"  │                │        │     server     │         │       │
 └────────────────┘            └────────────────┘        └────────────────┘         └───────┘
```

| Part | Where it is | What it does |
|------|-------------|--------------|
| **Leaf node** (one or more) | in the vineyard | measures, saves a copy on its memory card, sends the reading by radio |
| **Central node** | at the base, plugged into the Raspberry Pi | receives readings from every leaf node, confirms each one, passes it to the Pi |
| **Raspberry Pi** | at the base | stores readings in a database, and provides the Wi-Fi network and the app |
| **VitiSense app** | on a phone | shows the conditions and mildew alerts |

Each leaf node has three components attached to its circuit board: a
temperature and humidity sensor, a battery-backed clock, and a memory card.

## The journey of one reading

### 1. The leaf node wakes up and measures

Every 5 minutes the leaf node wakes from sleep, reads the temperature and
humidity from its sensor, and reads the date and time from its clock.

Each leaf node has its own number. Node 1 measures 10 seconds after the
5-minute mark, node 2 after 20 seconds, and so on. This spacing stops two
nodes from talking on the radio at the same moment.

### 2. It saves a copy on its memory card

Before doing anything else, the leaf node writes the reading as one new line
in a file on its memory card. The file is in *CSV* format, which opens
directly in Excel. There is one file per day:

```
leafNodeID,readingID,realTimeLog,temperature,humidity,windSpeed,leafWetness,status
5,812,1790940463,23.46,61.20,2.00,12,3
```

This copy is made first so that a reading is never lost because of a radio
problem. **The memory card is the complete record of everything the node has
measured.**

### 3. It packages the reading for the radio

The same reading is packed into a very short text message in *JSON* format:

```
{"n":5,"i":812,"t":1790940463,"T":23.46,"H":61.2,"W":2,"L":12,"f":7}
```

Each letter is a label: `n` is the node number, `i` the reading number, `t`
the time, `T` the temperature, `H` the humidity, and `f` a health code (see
[What a reading contains](#what-a-reading-contains)). The labels are kept to
single letters on purpose. A shorter message takes less time to transmit,
which saves battery and keeps the radio channel free for other nodes.

### 4. It sends the reading by radio

The message is sent using *LoRa*, a radio technology made for sending small
amounts of data over long distances on very little power. Sending one reading
takes less than half a second.

### 5. It waits for an acknowledgement

A radio message can be lost: by distance, an obstacle, interference, or two
nodes sending at once. So the leaf node does not assume the reading arrived.
After sending, it listens for just under a second for a reply from the
central node. That reply, the *acknowledgement* or *Ack*, says in effect
"central node here: I received reading 812 from node 5".

- **Ack received:** the reading is delivered. The node goes back to sleep.
- **No Ack:** the node waits a moment and sends the reading again, up to
  three times in total.

### 6. If there is still no acknowledgement, it queues the reading

A reading that was never acknowledged is put in a waiting list, the *queue*,
inside the leaf node. The node carries on measuring every 5 minutes as
normal. As soon as the central node answers again, the leaf node sends its
newest reading and then works through the queue, a few old readings at a
time, oldest first.

The queue holds 24 hours of readings. In other words, the central node or
the Raspberry Pi can be switched off for up to a day and the missing readings
will fill in by themselves afterwards. Readings older than that are still on
the memory card.

### 7. It goes back to sleep

The leaf node switches off its radio and screen and sleeps until the next
5-minute mark. It is normally awake for only a second or two in every five
minutes, which is what lets it run from a battery.

### 8. The central node receives, confirms and passes on

The central node listens all the time. When a reading arrives it:

1. checks that the message is complete and undamaged;
2. sends the Ack back to the leaf node straight away;
3. passes the reading to the Raspberry Pi through the USB cable, as one line
   of text, this time with full names and the strength of the radio signal
   added:

```
{"leafNodeID":5,"readingID":812,"realTimeLog":1790940463,"temperature":23.46,"humidity":61.2,"windSpeed":2,"leafWetness":12,"status":7,"rssi":-87.5,"snr":9.25}
```

If the same reading arrives twice (because the leaf node missed the Ack and
sent it again), the central node confirms it again but passes it on only once.

The central node does not store anything itself. Once a minute it also sends
the Pi a short "still alive" message, so that a quiet radio can be told apart
from a broken receiver.

### 9. The Raspberry Pi stores the reading

A program on the Pi reads each line from the central node, notes the time it
arrived, and saves the reading as a row in a *database*. It checks again that
the reading has not been stored already.

Readings are given to the app in the order they were **measured**, not the
order they **arrived**. A reading that waited in a leaf node's queue for
three hours still appears at the right place in the history.

### 10. The app shows it

The Raspberry Pi creates its own Wi-Fi network. A phone that joins it can
open the VitiSense app, which is delivered by the Pi itself, so the system
works in a vineyard with no internet connection. The Pi makes the stored
readings available to the app (the latest reading, the history, and the
health of the radio link), and the app compares temperature, humidity and
wind against published thresholds for powdery and downy mildew.

## Where the data is kept

| Copy | Where | Format | How long | Purpose |
|------|-------|--------|----------|---------|
| Memory card | each leaf node | *CSV* file, one per day | until the card is full or cleared | the complete record |
| Queue | each leaf node's memory | — | up to 24 hours, lost if the node restarts | readings waiting to be re-sent |
| Database | the Raspberry Pi | *SQLite* database | permanent | what the app shows; can be backed up as one file |

The same reading takes three forms on its journey:

| Stage | Format | Why |
|-------|--------|-----|
| On the memory card | CSV (one line of comma-separated values) | simple, opens in Excel |
| Over the radio | JSON with one-letter labels | as short as possible |
| From the central node to the Pi | JSON with full names | matches the database columns exactly |

## What a reading contains

| Item | Meaning |
|------|---------|
| Node number | which leaf node took the reading |
| Reading number | counts up by one for every reading the node takes, for its whole life |
| Time | when the reading was taken (see the note on time below) |
| Temperature | in °C |
| Humidity | in % |
| Wind speed, leaf wetness | **placeholders for now.** The sensors are not fitted yet, so fixed values are sent |
| Health code | whether the sensor worked, whether the clock can be trusted, whether the reading was saved to the memory card, and whether it was sent late from the queue |
| Signal strength | added by the central node: how well the radio message was received |

If a value could not be measured it is left blank. The system never fills in
a zero, because 0 °C or 0 % would look like a real measurement.

**A note on time.** All times are stored in *UTC*, which is two hours behind
South African time. Each leaf node's clock is set when the node is
programmed and keeps time with a small backup battery.

## What happens when something goes wrong

The system is built so that one failure does not stop everything else.

| If this happens | The system does this |
|-----------------|----------------------|
| The radio message is lost | the leaf node sends it again, up to three times |
| The central node or Raspberry Pi is off or out of range | the leaf node keeps measuring and saving to its card, queues the readings, and sends them when contact returns (up to 24 hours' worth) |
| The sensor on a leaf node fails | the node keeps reporting, with the temperature and humidity blank and the health code showing the fault. A failed sensor can therefore be told apart from a dead node |
| The memory card fails or is full | the node still sends by radio, and the health code shows the fault |
| The clock's backup battery runs flat | the node keeps measuring every 5 minutes but marks the time as unknown, rather than report a wrong time. The Pi's arrival time is used instead |
| A leaf node's program freezes | a built-in safety timer restarts the node within a minute, and the reason is noted on the memory card |
| The central node's radio stops listening | the central node resets its own radio, and restarts itself if it freezes |
| The same reading is delivered twice | the central node and the Pi each recognise it and keep one copy |

## Current limitations

- **Wind speed and leaf wetness are not measured yet.** Fixed placeholder
  values are sent and stored in their place.
- **The battery level of a leaf node is not reported.** A node with a flat
  battery simply stops sending.
- **An acknowledgement means the central node received the reading, not that
  the Raspberry Pi stored it.** If the Pi's software is stopped while the
  central node is still powered, readings sent in that time are confirmed but
  not stored. They remain on the leaf node's memory card.
- **A leaf node's queue is lost if the node restarts.** Those readings are
  still on its memory card, but have to be collected from the card by hand.
- **Clock accuracy.** Leaf node clocks are set from the computer used to
  program them and are accurate to within some tens of seconds.
- **The app is not yet connected to live readings.** At the time of writing
  the app shows built-in sample readings. The Pi already stores the real
  readings and makes them available; connecting the app's screens to them is
  the remaining step.
- **Radio range** in the vineyard still has to be confirmed with a field
  test.

## Glossary

| Term | Meaning |
|------|---------|
| **Leaf node** | a sensor station in the vineyard |
| **Central node** | the radio receiver at the base, plugged into the Raspberry Pi |
| **Raspberry Pi** | a small, low-cost computer |
| **LoRa** | "Long Range": a radio technology for sending small messages over long distances using very little power |
| **Acknowledgement (Ack)** | a short reply confirming that a message was received |
| **Queue** | a waiting list of readings that have not been confirmed yet |
| **CSV** | "comma-separated values": a plain text file with one record per line, which opens as a spreadsheet |
| **JSON** | a plain text format in which every value has a label, widely used to pass data between programs |
| **Database** | an organised store of records that programs can search and sort |
| **SQLite** | the database used on the Pi. The whole database is a single file |
| **UTC** | the world's standard reference time. South African time is UTC plus 2 hours |
| **PWA** | "progressive web app": an app delivered through the web browser that can be installed on a phone's home screen. VitiSense is one |

## More detail

Each part has its own technical documentation:

- Leaf node: [leaf_node_LoRa](https://github.com/Toast1e/leaf_node_LoRa)
- Central node: [central_node_LoRa](https://github.com/Toast1e/central_node_LoRa)
- Raspberry Pi (database, app server, installation): [README.md](README.md)
- App: [central_node_pwa](https://github.com/Toast1e/central_node_pwa)
