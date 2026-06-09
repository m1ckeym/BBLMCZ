.Z80
;     NOTE ******  USE M80  ASSEMBLER
;
;**************************************************************
;*
;*     PROCEDURE:      BAUD.ASM
;*     PURPOSE:        TO SET UP THE SERIAL PORT FOR
;*                     1200 BAUD, 1 STOP, ODD PARITY
;*     PROGRAMMER:     CHARLES M. LINK
;*     DATE:           JANUARY 28, 1982
;*     FOR:            BBL INDUSTRIES, INC.
;*
;**************************************************************
;
SRCTL1  EQU     91H
CLKA1   EQU     0B1H
;
        ORG     100H
;
BEGIN:  DI                      ;DISABLE THE INTERRUPTS
        LD      A,04            ;FOR 1200 BAUD
        OUT     (CLKA1),A       ;TO CTC
;
;
;NOW SET UP THE SIO
;
        LD      C,SRCTL1        ;POINT TO THE CONTROL PORT
        LD      HL,INIOUT       ;SET UP FOR A BLOCK MOVE
        LD      B,OUTCNT        ;NUMBER OF BYTES TO SEND
        OTIR
        EI
        RET                     ;RETURN TO CPM
;
;
INIOUT: DEFB    18H             ;RESET SIO
        DEFB    0               ;IDLE
        DEFB    4               ;REG 4
        DEFB    01000101B       ;16X CLK, ENABLE PARITY, ODD, 1STOP, ASYNC
        DEFB    3               ;REG 3
        DEFB    01000001B       ;RECEIVE ENABLE, 7 BITS/CHAR, DTR
        DEFB    5               ;REG 5
        DEFB    10101010B       ;TX ENABLE
        DEFB    1               ;REG 1
        DEFB    0               ;NO INTERRUPTS
OUTCNT  EQU     $-INIOUT
        END
