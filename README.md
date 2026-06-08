# BBLMCZ

My goal is to revive a computer that I used in the mid-80's. It was a beast in a 12U box and I couldn't keep
the power supplies and disk drives, but I was able to save the core circuit boards. Three of the four boards
came from the BBL System III paging system:

The PC980 Z80 CPU
![Alt text](boards/images/cpu980.jpg)

The PC964 64K Dynamic RAM
![Alt text](boards/images/mem64k1.jpg)

The PC964 4-channel SIO
![Alt text](boards/images/sio.jpg)


The fourth card was made custom for the BBL Computer System. It was nearly a direct clone of the Zilog MCZ
disk controller and monitor PROM:

The PC971 Disk Controller
![Alt text](boards/images/diskboard1.jpg)

It has a daughter card for the FM data separator. It has a missing chip for an additional hurdle:
![Alt text](boards/images/diskboard2a.jpg)


The four cards in a crude cage:
![Alt text](boards/images/cagefront.jpg)

The backplane is wire-wrapped like the original:
![Alt text](boards/images/cagerear.jpg)



First milestone was to get the PROM monitor working:

    >r
    A  B  C  D  E  F  H  L  I  A' B' C' D' E' F' H' L'  IX   IY   PC   SP  
    FF 00 00 10 00 28 60 00 00 00 F6 F4 DF D9 28 F5 55 E734 129E 0000 D218 
    >

Time passes, figuring out how to get a disk drive connected and how to write it...

    I used MCZIMAGER as a base to make BBLIMAGER
    >j 8000

    BBLIMAGER V10.4 - BBL MCZ FLOPPY IMAGER
    G=READ DISK (HEX)  O=WRITE DISK (HEX)
    D=DUMP SEC  B=BURN SEC  R=PROM RD  W=PROM WR
    F=FAST ERASE (DIRECT)  Z=FORMAT DISK (PROM WRITE+VERIFY)
    V T/D [TT] = BULK ERASE TRACK/DISK (RAW ZEROS, NO SECTOR SYNC)
    E S/T/D [TT SS] = ERASE  P S/T/D [TT SS] = DUMP
    X S/T/D [TT SS] = WRITE (HEX LINES)  Q=QUIT

    BBL>

So far it loads the boot sector from track 23 sector 3 into memory:

    >l
    >d 1000 80
    1000 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F 10 11 12 *................*
    1010 13 14 15 16 17 18 19 1A 1B 1C 1D 1E 1F 20 21 22 *............. !"*
    1020 23 24 25 26 27 28 29 2A 2B 2C 2D 2E 2F 30 31 32 *#$%&'()*+,-./012*
    1030 33 34 35 36 37 38 39 3A 3B 3C 3D 3E 3F 40 41 42 *3456789:;<=>?@AB*
    1040 43 44 45 46 47 48 49 4A 4B 4C 4D 4E 4F 50 51 52 *CDEFGHIJKLMNOPQR*
    1050 53 54 55 56 57 58 59 5A 5B 5C 5D 5E 5F 60 61 62 *STUVWXYZ[\]^_`ab*
    1060 63 64 65 66 67 68 69 6A 6B 6C 6D 6E 6F 70 71 72 *cdefghijklmnopqr*
    1070 73 74 75 76 77 78 79 7A 7B 7C 7D 7E 7F 80 81 82 *stuvwxyz{|}~....*
    >

Now it needs a good boot disk... Weeks later:

I patched a Zilog boot disk for the different serial I/O and ...

    It BOOTED! Sort of:

    >L
    INVALID HRDWR. CNTCT ZILOG

    That was the PROM's message, this is the OS boot:

    >OS
    INVALID HARDWARE. CONTACT ZILOG


So I went off to make a CP/M disk from scratch... And much later...

Now I can send MCZ image files to it and make a disk:

    >l

    BBL CP/M BIOS 1.3N
    CP/M 2.2 Copyright (C) 1979, Digital Research
    TPA:E300H (56K)
    WRITE:option3 inline F920  READ:retry F820
    CONOUT:TXRDY  PAGE0:WBOOT=BIOS table
    WBOOT:reload DB80-E3FF  APPS:WordStar 3.00
    DHTXS
    A>dir
    A: HELLO1   COM : PLAYASC  COM : SWMCZ    DAT : LOAD     COM
    A: TEST     HEX : IMAGER   COM : BIG32    COM : STAT     COM
    A: PIP      COM : DTEST    COM : IRA          : RTEST    COM
    A: DPBTEST  COM : RPROBE   COM : WTEST15  COM : WT15FS   TXT
    A: WLOG     COM : WPROBE   COM : WEN      COM
    A>hello1

    HELLO1.COM FROM BLOCK 01H / T03:S10HB
    A>

It's fragile but it works.

Then I recovered the original CP/M from and old floppy and added some games:
    
    $ time python3 BBLclient5.py /dev/ttyUSB1 --baud 9600 --tx-char-ms 0 --tx-line-ms 15  --mode companion  --write-disk '/home/mickeym/Downloads/bbl_mcz_cpm_boot_test_v17_adventure_mbasic_startrek.mcz' 

    BBL>[xd] image: /home/mickeym/Downloads/bbl_mcz_cpm_boot_test_v17_adventure_mbasic_startrek.mcz (335104 bytes)
    [xd] pacing: 0ms/char  15ms/line
    [xd] sending XD command to companion...
    xd
    >
    [xd] got ready signal, starting transfer...
    [00] T00 >[01] T01 >[02] T02 >[03] T03 >[04] T04 >[05] T05 >[06] T06 >[07] T07 >[08] T08 >[09] T09 >[0A] T0A >[0B] T0B >[0C] T0C >[0D] T0D >[0E] T0E >[0F] T0F >[10] T10 >[11] T11 >[12] T12 >[13] T13 >[14] T14 >[15] T15 >[16] T16 >[17] T17 >[18] T18 >[19] T19 >[1A] T1A >[1B] T1B >[1C] T1C >[1D] T1D >[1E] T1E >[1F] T1F >[20] T20 >[21] T21 >[22] T22 >[23] T23 >[24] T24 >[25] T25 >[26] T26 >[27] T27 >[28] T28 >[29] T29 >[2A] T2A >[2B] T2B >[2C] T2C >[2D] T2D >[2E] T2E >[2F] T2F >[30] T30 >[31] T31 >[32] T32 >[33] T33 >[34] T34 >[35] T35 >[36] T36 >[37] T37 >[38] T38 >[39] T39 >[3A] T3A >[3B] T3B >[3C] T3C >[3D] T3D >[3E] T3E >[3F] T3F >[40] T40 >[41] T41 >[42] T42 >[43] T43 >[44] T44 >[45] T45 >[46] T46 >[47] T47 >[48] T48 >[49] T49 >[4A] T4A >[4B] T4B >[4C] 
    [xd] all sectors sent, waiting for DONE...
    T4C 
    DONE
    
        BBL>[xd] disk write complete!
    
        real	41m57.989s
        user	0m32.055s
        sys	0m23.034s

And I have the original bootloader and CP/M BIOS running!

    >l

    64K CP/M VERS 2.2

    A>dir
    A: HELLO    COM : BIG32    COM : README   TXT : SECTORS  TXT
    A: BOOTNOTE TXT : LOAD     COM : TEST     HEX : PIP      COM
    A: IMAGER   COM : STAT     COM : OPENTST  COM : ADV      COM
    A: ADVI     DAT : ADVI     PTR : ADVT     DAT : ADVT     PTR
    A: DECODE   DAT : MBASIC   COM : STARTREK BAS : TREKINST BAS
    A>stat
    A: R/W, Space: 15k

    A>


    


