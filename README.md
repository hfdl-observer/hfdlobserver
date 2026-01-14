# HFDLObserver

A multi-headed dumphfdl receiver for use with Web-888 devices and other SoapySDR-compatible receivers.

> [!IMPORTANT]
> If you are currently using a version from this repository when the main script was called `hfdlobserver888.sh`, see the next to last section of this document: *Upgrading from HFDLObserver888* for important information. This version is not directly compatible with the settings and environment of the `HFDLObserver888.sh` installation. You will have to perform a migration step.


## Background

The dynamism of the High Frequency Data Link infrastructure poses some problems for those trying to efficiently monitor these packets. There are a wide variety of frequencies in use across the HF spectrum (in this case, between 2.5 and 22MHz). The active frequencies change depending on time of day, ionospheric conditions, and the associated ground station. Picking the correct frequencies, and switching between them is a challenge. This challenge is magnified as many available SDRs have limited sample rates, and cannot scan the entire available HFDL frequency space.

There are several approaches to solving this problem. A common one is to use some script or helper to change the frequencies your SDR listens to according to some rules. This could be based on regular brief empirical scans of activity across the whole HF spectrum, or picking frequencies based on active station announcements. These work okay for single SDR set ups. Managing multiple receivers to cover more of the possible (or even merely declared active) frequencies is usually achieved by directly assigning different SDRs to specific ranges.

### RX-888

[RX-888 (mk2)](https://www.rx-888.com/rx/) and [Web-888](https://www.rx-888.com/web/) devices are advanced, yet still affordable, SDRs that can process the entire HF spectrum at the same time. This makes them very attractive to those wanting to operate an HFDL listening station. Unfortunately, HFDL stations are frequently lower power devices such as odroids, raspberry pi and the like which may not have the CPU necessary to deal with the firehose of data from the RX-888 mk2.

The Web-888 offers a solution for that. It combines the receiver of an RX-888 mk2 with an FPGA programmed to do bulk processing of data, and an KiwiSDR compatible web interface, based on [Beagle SDR](https://github.com/jks-prv/Beagle_SDR_GPS) from which KiwiSDR is also derived. It offers 13 simultaneous streams/channels to process. Each channel is fairly narrow (12kHz) but that is enough to cover up to 4 HFDL frequencies (if they are close enough together).

## Features

HFDLObserver helps with these problems by managing frequency assignments for an arbitrary number of diverse radios. The simplest, default case is based on the Web-888 device described above. HFDLObserver makes using a Web-888 device to receive HFDL packets (and share them with airframes.io, or other consumers) easy.

Optionally, it can also retrieve updated frequency lists from community source (such as hfdl.observer or airframes.io). This covers periods where squitters (frequency updates) may not be received by your station for a time.

In general, there are around 30 frequencies active globally at a given time.

### Web-888 receivers

It assigns frequencies to each of 13 virtual "receivers". The assignments are based on a list of currently active frequencies ranked by a user-configured station preference (generally stations nearer your Web-888 should have higher preference).

It manages the `kiwirecorder` web socket clients that stream the raw data from Web-888 for each virtual receiver. It also manages the `dumphfdl` processes used to decode HFDL from the raw I/Q data `kiwirecorder` emits.

It watches the decoded HFDL packets for frequency updates. When the active frequency list changes, virtual receivers may be reassigned to higher-priority frequencies.

Processing the entire HF frequency space would be very CPU-intensive. Taking advantage of the FPGA in Web-888 to select only the portions we're interested in means:

- The data rate from the Web-888 to the device running HFDLObserver is around 5Mbps.
- The aggregate bandwidth that needs to be scanned by all virtual receivers is around 156kHz.
- The CPU required for the virtual receivers is about ½ of 1 core of an Odroid M1S or Raspberry Pi4 (~13% total CPU)

HFDLObserver allows your station to monitor (typically) 18-23 active HFDL frequencies on a single Web-888 appliance. In addition, where possible, HFDLObserver will watch neighbouring inactive but assigned frequencies. These "extras" typically add little if anything to packet counts, but contribute to completeness.

### Other radios

Several other SDRs are supported by HFDLObserver, though these require manual setup, as `hfdlobserver.sh configure` does not know about them (yet). Each of these requires SoapySDR support to be built into `dumphfdl`, and this is not something that the default installer will do.

### Airsphy HF+ range

This is managed using the [SoapyAirspyHF](https://github.com/pothosware/SoapyAirspyHF) driver, so this must be present for an HFDLObserver receiver to use it.

### SDRPlay devices

This is likewise handled by the [SoapySDRPlay3](https://github.com/pothosware/SoapySDRPlay3) driver, so this must be present for an HFDLObserver receiver to use it. In addition, this requires the SDRPlay system service/driver to be available. This driver has proved very problematic on Linux. HFDLObserver tries to mitigate this by watching for errors and restarting (as carefully as a user space app can) the receiver on several error conditions. It is very good to be quite conservative in configuring SDRPlay devices, as they can easily saturate the isochronous USB-2 bus. In addition, keeping maximum sample rates to 6MS/s or less is highly recommended, as above this, the dynamic range is steadily reduced.

Current known working SDRPlay devices are:
- SDRPlay RSPdx

### RX-888mk2

As mentioned above, this is a great wideband SDR receiver. Its Linux driver support is not great, but the SoapySDR driver in the [ExtIO_sddc](https://github.com/ik1xpv/ExtIO_sddc) project works with HFDLObserver and `dumphfdl` fairly well. For reasons not currently understood by HFDLObserver, this driver and `dumphfdl` fail to work together above 8MS/s. But that's still a nice wide bandwidth.


## Advanced Features

HFDLObserver is evolving software. As such, it has some more advanced features that are in various stages of stability, and which generally require more expert handling for the time being. If you want to try to use one of these, drop by the [Airframes.io](https://airframes.io) Discord, in the "#𝐇𝐅〉hfdl" channel.

Additional topics, such as details of most of the available settings can be found in the [Advanced Topics](extras/advanced-settings.md) documentation.


### Other SoapySDR devices

Hypothetically, any device with a SoapySDR driver should be usable under HFDLObserver, though they may take some additional advanced configuration.

### Remote Devices

An experimental addition to HFDLObserver is a simple network protocol for managing devices running on other "nodes" (computers). This allows you to spread high bandwidth devices across your home network. This involves installing HFDLObserver on those computers and running them in a special "node" mode. While it requires only settings changes, they're not currently documented well (coming eventually).

### Receiver-less operation

If you just want to use the CUI (especially the heat map) to monitor the output of many SDRs you're managing some other way, this is also possible (and not even that advanced, really). HFDLObserver gathers its packet information over a UDP port from the various decoders (`dumphfdl`) and there's no actual need for HFDLObserver to control any of the receivers itself.


## CUI

HFDLObserver also adds a simple but rich console-based display. At the top is a heat map like grid depicting the frequencies currently (or recently) being observed, and packet counts for each minute. Below that is log output. As it is console based, it can run within a `screen` session over `ssh` from a remote computer.

This is a bit more CPU intensive. On a web-888 connected system, it takes about the same CPU as all of the virtual receivers combined. It can be disabled, and is disabled by default when it is run as a (systemd) service.

The packet count from each bin is represented by `1` to `9`, then by `a` to `z` for 10 through 35, and `A` to `Z` for 36 through 61. The default bin size is 60 seconds, and it's highly unlikely you'll ever see anything above that. However, if you configure the bin size manually, you will see `✽` for such packet counts. The bin background is coloured based on the packet count, from blue through green, yellow, orange, red, and magenta to purple. This scales with the maximum packet count needed, so even when you have max packet counts, you will have *some* indication of relative density.

![HFDL Observer CUI](https://github.com/user-attachments/assets/276c540b-e8ab-4bff-a726-35f77d24344f)


### Space Weather

On the top line is the current up time for the app. To its left is the current Space Weather dashboard. It is the same data shown on the [NOAA Space Weather Enthusiats Dashboard](https://www.swpc.noaa.gov/communities/space-weather-enthusiasts-dashboard). There's an explanation of the scales there. This is useful for a quick glance to diagnose reception issues. They are a bit condensed here. In each group:

- R = Radio Blackouts
- S = Solar Radiation Impact
- G = Geomagnetic Storm Impact

`R2|S0|G0` The first group describes the recent (24h) maxima of indicators. Generally if there is a number higher than 0 in any of these, especially "R", that may explain unusual reception numbers in the recent past.

`R0|S0|G0` The second group describes "current" conditions. Note that this isn't always completely relevant to your precise location given the rotation of the earth.

`R70/30|S15|G0` This is a forecast for the next 24 hours. The "R" number gives the percent chance for "major" and "minor" events. The "S" number gives a percent chance for the "S" rating to rise above 0. The "G" number is the forecast level of Geomagnetic Storm Impact (this is the level, not a probability).

### Cumulative Stats

The next line provides an overview of the total stats since the app started.

- ⏬ packets received from aircraft
- ⏫ packets received from ground stations
- 🌐 packets with positions
- ❔ packets without positions
- 📰 squitter (network update) packets received
- 🔎 number of frequencies being observed out of the total active frequencies
- 📶 total number of packets (The number first is the packets since the app was last started. The second is the total packets in the last day. See also the Advanced Settings document for further options.)


### Command Keys

The HFDLObserver CUI allows limited control of the display at runtime with a few keystrokes:

- `[,]` - previous display mode
- `[.]` - next display mode
- `[-]` - bin size -60s
- `[+]` - bin size +60s
- `[1]` - frequency display
- `[2]` - band display
- `[3]` - station display
- `[4]` - agent display
- `[5]` - receiver display
- `[h]` - this help
- `[q]` or `[^C]` - quit


## Setting up the Web-888

To start, follow the [basic set up instructions](https://www.rx-888.com/web/guide/requirements.html) on the Web-888 site. You'll need to put the ROM image on a micro-SD card. There's little activity and little use of space, so you should not go overboard on a card (in fact, don't use anything 32GB or larger, as the device will be unable to read it).

You do not have to configure any of the "public" options -- you aren't going to be sharing this device to the public. You should make sure its location is configured correctly, though. This can be done automatically if you've attached a GPS antenna.

There are only a few settings that are of interest.

### Control Tab

- `HF Bandwidth Selection`: select 32M. Using 64M will disable 1 channel (leaving only 12).
- `Disable waterfalls/spectrum?`: YES. No web clients will be using this device, and you can save a bit of processing power.
- `Switch between HF or Air Band`: Select HF

### Config Tab

- `Enable ADC PGA?`: your choice. It's safe to try either for a period.
- `Correct ADC clock by GPS PPS`: YES if you have a GPS antenna connected.
- `Enable ADC Dithering`: NO. This does not help the I/Q processing dumphfdl does.

### Public Tab

- `Register on www.rx-888.com/web/rx?`: NO. You're using this device exclusively for your own private use. Even if you need to access it over public Internet, you don't need it to register with the available public servers.


## Installation

Installation can be performed on `apt`-equipped systems (Debian, Ubuntu, Armbian, etc.) by using the provided `install.sh` command. The installation requires `sudo` access so that it can install packages and dependencies.

```
$ git clone https://github.com/hfdl-observer/hfdlobserver
$ cd hfdlobserver
$ ./install.sh
```

Formal releases are not made at this time, so `main` off of the repository is the best source. Releases will come eventually.

### Breakdown

The install script automates the following steps:

1. Installing necessary basic packages: `whiptail python3 python3-venv git`
2. Set up a virtual environment, and activate it.
3. Install Python requirements (from `requirements.txt`) into the virtual environment using `pip`.
4. Download `kiwiclient` to a known location.
5. Install `dumphfdl` (and dependencies)
   1. Install package dependencies: `build-essential cmake pkg-config libglib2.0-dev libconfig++-dev libliquid-dev libfftw3-dev zlib1g-dev libxml2-dev libjansson-dev`
   2. clone `libacars`, build, and install it.
   3. clone `statsd-c-client`, build, and install it.
   4. clone `dumphfdl`, build, and install it.
6. Run `./configure.py` to walk through some simple configuration questions.

While several helper programs are installed, they are invoked via the operating system, HFDLObserver makes no alteration to any of their code or operations, and connects only through standard mechanisms (file handles and sockets).

## Configuration

Configuration is provided by a YAML formatted file. By default, and normally, it is `settings.yaml`.

The provided configurations script asks a number of questions to provide basic configuration. For most users, this should suffice. The `src/settings.py` file contains a commented system default settings dialog for the curious, or those in need of more complex configurations. This is still in some flux, but the basic `settings.yaml` structure should be stable.

You can rerun the configuration script at any time by running `hfdlobserver.sh configure` from the install directory, and it will walk you through the questions again; subsequent runs will write to `settings.yaml.new` so you can compare and merge the files if you desire.

The configuration tool provides two options for setting the ranked order of HFDL stations.

1. You can provide a comma-separated list of station IDs. You can see the station IDs and some related information at the [HFDL.observer](https://hfdl.observer) site.

2. The configuration tool can "guess" the station ranking. It builds this list using distance from your Web-888's location. You will have to enter it. Generally entering just the rounded degrees latitude and longitude should be sufficient.

The distance tool is also available as

```
$ extras/guess_station_ranking.py <lat> <lon>
```

## Running

Once configured, you can run the receiver by

```
$ <path-to>/hfdlobserver.sh
```

if you do not want the "fancy" TUI, pass in the `--headless` option. The usage is minimal, but is explained with `--help`.

Hopefully, it should Just Work.

In case of abnormal termination, you should kill any `kiwirecorder.py` and `dumphfdl` instances that may be left hanging. This can be accomplished with the following:

```
$ pkill -f kiwirecorder ; pkill -f dumphfdl
```

## Exiting

Press `^C` (control + C). Enhance your calm, as it can take several seconds to shut down cleanly. At present, seeing exception traces at exit is not unusual; don't worry as these are normally harmless.

## Run as a Service (very alpha)

If you want to run this as a service, you can run the script to install the service file for systemd.

```
$ extras/install-service.sh
```

It then becomes a normal service named `hfdlobserver`. Following the usual pattern, there is a very minor ability to configure it via `/etc/default/hfdlobserver`, but most items are managed through the `settings.yaml` file.


## Upgrading from HFDLObserver888

The original versions of HFDLObserver (as `HFDLObserver888`) use a different structure for settings. Additionally, this new version adds some dependencies. If you are using the older version and want to update (recommended!) you need to perform a couple of steps

1. **save your original `settings.yaml` some place, just in case.**
2. stop any current `hfdlobserver888` instances that are running on the machine.
3. `cd` into the install directory.
4. use `git pull` to update the code.
5. run `extras/migrate-from-888.sh`
6. run `./hfdlobserver.sh configure`
7. copy `settings.yaml.new` to `settings.yaml` (if you have been using a simple setup)
8. run `./hfdlobserver.sh` to enjoy the new code.


## Acknowledgements

- [dumphfdl](https://github.com/szpajder/dumphfdl) - an excellent decoder of HFDL signals
- [kiwiclient](https://github.com/jks-prv/kiwiclient) - used to stream data from Web-888 to dumphfdl
- [airframes.io](https://airframes.io/) - a great community of people interested in data from the sky.
- [libacars](https://github.com/szpajder/libacars) - used by dumphfdl to parse ACARS data from HFDL packets
- [stats-d-client](https://github.com/romanbsd/statsd-c-client.git) - used to optionally send statsd statistics.
