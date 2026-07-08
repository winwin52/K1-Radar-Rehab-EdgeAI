/**
 **************************************************************************************************
 * @brief   project config define.
 * @attention
 *
 * Copyright (C) 2025 POSSUMIC TECHNOLOGY CO., LTD. All rights reserved.
 *
 *  Redistribution and use in source and binary forms, with or without
 *  modification, are permitted provided that the following conditions
 *  are met:
 *    1. Redistributions of source code must retain the above copyright
 *       notice, this list of conditions and the following disclaimer.
 *    2. Redistributions in binary form must reproduce the above copyright
 *       notice, this list of conditions and the following disclaimer in the
 *       documentation and/or other materials provided with the
 *       distribution.
 *    3. Neither the name of POSSUMIC TECHNOLOGY CO., LTD. nor the names of
 *       its contributors may be used to endorse or promote products derived
 *       from this software without specific prior written permission.
 *
 *  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 *  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 *  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
 *  A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
 *  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 *  SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 *  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 *  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 *  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 *  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 *  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 *
 **************************************************************************************************
 */


#ifndef _PRJ_CONFIG_H
#define _PRJ_CONFIG_H


/*
 * The board supported by the SDK,
 * please refer to platfromr/boards/board_config.h,
 * MRS6130-P1806 will use uart0 as HIF and uart1 as printf com port，
 * MRS6130-P1812/MRS6240-P2512 will use uart0 as printf & shell, SPI or UART1 as HIF
 */
#if CONFIG_BOARD_MRS6130_P1806
#define CONFIG_PRINTF_UART_NUM                              1
#else
#define CONFIG_PRINTF_UART_NUM                              0
#endif

/* Upload DataCube will cost more time */
#define CONFIG_R3_DATA_BOX_UPLOAD_DATA_CUBE                 0
#if CONFIG_R3_DATA_BOX_UPLOAD_DATA_CUBE
#define HEAP_SZIE_DATA_CUBE									(1024 * 12) /* data cube upload need 12K sram cache data */
#else
#define HEAP_SZIE_DATA_CUBE									(1024 * 0)
#endif

/* Upload 1Dfft Data will cost more time and buffer
 * if CONFIG_R3_DATA_BOX_UPLOAD_1DFFT_DATA == 1,frame period need config 100ms */
#define CONFIG_R3_DATA_BOX_UPLOAD_1DFFT_DATA                1	
#if CONFIG_R3_DATA_BOX_UPLOAD_1DFFT_DATA
#define HEAP_SZIE_1D_CUBE									(1024 * 16) /* if range fft = 512,need sram equal 512 * 4 * MIMO_RX_NUM(6240 is 8) = 16K */
#else
#define HEAP_SZIE_1D_CUBE									(1024 * 0)
#endif

/* STATIC CONFIG USE SOFTWARE CFAR OR NOT */
#define SW_CFAR_ENABLE										0

/* Enable micro point out by default */
#define CONFIG_MMW_MICRO_POINT_CLOUD						1
#if CONFIG_MMW_MICRO_POINT_CLOUD
#define HEAP_SZIE_MiCRO										(1024 * 78) /* micro motion need more sram */
#else
#define HEAP_SZIE_MiCRO										(1024 * 0)
#endif

#define HEAP_SZIE_BASIC										(1024 * 55) /* just running motion point cloud need 55K */
#define CAL_HEAP_SZIE_TOTAL									HEAP_SZIE_DATA_CUBE + HEAP_SZIE_MiCRO + HEAP_SZIE_1D_CUBE
#define CONFIG_HEAP_SIZE 									CAL_HEAP_SZIE_TOTAL + HEAP_SZIE_BASIC

#define CONFIG_MMW_CTRL                                     1
#define CONFIG_MMW_DRIVER                                   1
#define CONFIG_MMW_CALIB_DATA_LOAD                          1 /* load from flash */

#define CONFIG_HIF                                          1
#if CONFIG_BOARD_MRS6130_P1806
#define CONFIG_HIF_UART_PORT                                0
#else
#define CONFIG_HIF_UART_PORT                                1
#endif
#define CONFIG_HIF_DEVICE_COM_UART                          1
#define CONFIG_HIF_DEVICE_WAKEUP_UART                       1

#define CONFIG_HIF_DEVICE_COM_SPI                           1
#define CONFIG_HIF_DEVICE_WAKEUP_SPI                        1

#define CONFIG_HIF_DMA_PINGPONG                             0
#define CONFIG_HIF_SEND_DMA                                 1

#define CONFIG_PM                                           0 /* PM is not support yet. */
#define CONFIG_PMU_EXTLDO_SLEEP_SW_SUBMODE                  1

#endif /* _PRJ_CONFIG_H */

/*
 **************************************************************************************************
 * (C) COPYRIGHT POSSUMIC TECHNOLOGY
 * END OF FILE
 */
