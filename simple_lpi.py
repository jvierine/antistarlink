import os
# mpi does paralelization, both multithread matrix operations
# just one per process to avoid cache trashing.
os.system("export OMP_NUM_THREADS=1")
os.environ["OMP_NUM_THREADS"] = "1"

import numpy as n
import matplotlib.pyplot as plt
import pyfftw
import stuffr
import scipy.signal as s
from digital_rf import DigitalRFReader, DigitalMetadataReader, DigitalMetadataWriter

import h5py
from scipy.ndimage import median_filter
from scipy import sparse
from mpi4py import MPI
import scipy.constants as c
import traceback
import time

comm=MPI.COMM_WORLD
size=comm.Get_size()
rank=comm.Get_rank()

# we really need to be narrow band to avoid interference. there is plenty of it.
# but we can't go too narrow, because then we lose the ion-line.
# 1000 m/s is 3 kHz and the code itself is 66.6 kHz
# 66.6/2 + 3 = 70 kHz, and thus the maximum frequency offset will be 35 kHz.
# that is tight, but hopefully enough to filter out the interference.
#pass_band=0.1e6

def ideal_lpf(z,sr=1e6,f0=1.2*0.1e6,L=200):
    m=n.arange(-L,L)+1e-6
    om0=n.pi*f0/(0.5*sr)
    h=s.hann(len(m))*n.sin(om0*m)/(n.pi*m)

    Z=n.fft.fft(z)
    H=n.fft.fft(h,len(Z))
    z_filtered=n.roll(n.fft.ifft(Z*H),-L)

    return(z_filtered)

class simple_decimator:
    def __init__(self,L=10000,dec=10):
        self.L=L
        self.dec=dec
        decL=int(n.floor(L/dec))
        self.idxm=n.zeros([decL,dec],dtype=int)
        for ti in range(decL):
            self.idxm[ti,:]=n.arange(dec,dtype=int) + ti*dec
    def decimate(self,z):
        decL=int(n.floor(len(z)/self.dec))
        return(n.mean(z[self.idxm[0:decL,:]],axis=1))

class fft_lpf:
    def __init__(self,z_len=10000,sr=1e6,f0=1.2*0.1e6,L=20):
        m=n.arange(-L,L)+1e-6
        om0=n.pi*f0/(0.5*sr)
        h=s.hann(len(m))*n.sin(om0*m)/(n.pi*m)
        # normalize to impulse response to unity.
        h=h/n.sum(n.abs(h)**2.0)
        self.H=n.fft.fft(h,z_len)
        self.L=L
        
    def lpf(self,z):
        return(n.roll(n.fft.ifft(self.H*n.fft.fft(z)),-self.L))
        
        


def ideal_lpf_h(sr=1e6,f0=1.2*0.1e6,L=200):
    m=n.arange(-L,L)+1e-6
    om0=n.pi*f0/(0.5*sr)
    h=s.hann(len(m))*n.sin(om0*m)/(n.pi*m)
    return(h)

def estimate_dc(d_il,tmm,sid,channel):
    # estimate dc offset first
    z_dc=n.zeros(10000,dtype=n.complex64)
    n_dc=0.0
    for keyi,key in enumerate(sid.keys()):
        if sid[key] not in tmm.keys():
            print("unknown pulse, ignoring")
        # fftw "allocated vector"
        z_echo = d_il.read_vector_c81d(key, 10000, channel)
        last_echo=tmm[sid[key]]["last_echo"]        
        gc=tmm[sid[key]]["gc"]
        
        z_echo[0:(gc+4000)]=n.nan
        z_echo[last_echo:10000]=n.nan
        z_dc+=z_echo
        n_dc+=1.0
    z_dc=z_dc/n_dc

    if False:
        # plot the dc offset estimate
        plt.plot(z_dc.real)
        plt.plot(z_dc.imag)
        plt.axhline(n.nanmedian(z_dc.real))
        plt.axhline(n.nanmedian(z_dc.imag))
        print(n.nanmedian(z_dc.real))
        print(n.nanmedian(z_dc.imag))        
        plt.show()
    
    z_dc=n.complex64(n.nanmedian(z_dc.real)+n.nanmedian(z_dc.imag)*1j)
    return(z_dc)
    

def convolution_matrix(envelope, rmin=0, rmax=100):
    """
    we imply that the number of measurements is equal to the number of elements
    in code

    Use the index matrix (idxm) to efficiently grab the numbers from a 1d array to build a matrix
    A = z_tx_envelope[idxm]
    """
    L = len(envelope)
    ridx = n.arange(rmin, rmax,dtype=int)
    A = n.zeros([L, rmax - rmin], dtype=n.complex64)
    idxm = n.zeros([L, rmax - rmin], dtype=int)    
    for i in n.arange(L):
        A[i, :] = envelope[(i - ridx) % L]
        idxm[i,:] = n.array(n.mod( i-ridx, L ),dtype=int)
    result = {}
    result["A"] = A
    result["ridx"] = ridx
    result["idxm"] = idxm
    return(result)

tmm = {}
T_injection=1172.0 # May 24th 2022 value
tmm[300]={"noise0":7800,"noise1":8371,"tx0":76,"tx1":645,"gc":1000,"last_echo":7700,"e_gc":800}
for i in range(1,33):
    tmm[i]={"noise0":8400,"noise1":8850,"tx0":76,"tx1":624,"gc":1000,"last_echo":8200,"e_gc":800}


def lpi_files(dirname="/media/j/fee7388b-a51d-4e10-86e3-5cabb0e1bc13/isr/2023-09-05/usrp-rx0-r_20230905T214448_20230906T040054",
              avg_dur=10,  # n seconds to average
              channel="zenith-l",
              rg=60,       # how many microseconds is one range gate
              output_prefix="lpi_f",
              min_tx_frac=0.5,  # how much of the pulse can be missing due to ground clutter clipping, defines the minimum range gate
              reanalyze=False,
              pass_band=0.1e6,
              filter_len=20,
              use_long_pulse=True,
              maximum_range_delay=7000    # microseconds. defines the highest range to analyze
              ):

    os.system("mkdir -p %s"%(output_prefix))
    
    id_read = DigitalMetadataReader("%s/metadata/id_metadata"%(dirname))
    d_il = DigitalRFReader("%s/rf_data/"%(dirname))

    idb=id_read.get_bounds()
    # sample rate for metadata
    idsr=1000000
    # sample rate for ion line channel
    sr=1000000

    plot_voltage=False
    use_ideal_filter=True
    debug_gc_rem=False

    # how many integration cycles do we have
    n_times = int(n.floor((idb[1]-idb[0])/idsr/avg_dur))

    # which lags to calculate
    lags=n.arange(1,47,dtype=int)*10
    # how many lags do we average together?
    lag_avg=2

    # calculate the average lag value
    n_lags=len(lags)-lag_avg
    mean_lags=n.zeros(n_lags)
    for i in range(n_lags):
        mean_lags[i]=n.mean(lags[i:(i+lag_avg)])

    # maximum number of microseconds of delay, which we analyze
    # this is experiment specific. need to read from configuration evenetually
    
    n_rg=int(n.floor(maximum_range_delay/rg))
    rgs=n.arange(n_rg)*rg
    rmax=n_rg

    # round trip speed of light in vacuum propagation, one microsecond
    rg_1us=c.c/1e6/2.0/1e3

    # range gates
    rgs_km=rgs*rg_1us

    # first entry in tx pulse metadata
    i0=idb[0]

    lpf=fft_lpf(10000,f0=1.2*pass_band,L=filter_len)

    decim=simple_decimator(L=10000,dec=rg)

    # go through one integration window at a time
    for ai in range(rank,n_times,size):
        
        i0 = ai*int(avg_dur*idsr) + idb[0]

        if os.path.exists("%s/lpi-%d.png"%(output_prefix,int(i0/1e6))) and reanalyze==False:
            print("already analyzed %d"%(i0/1e6))
            continue

        # get info on all the pulses transmitted during this averaging interval
        # get some extra for gc
        sid = id_read.read(i0,i0+int(avg_dur*idsr)+40000,"sweepid")

        n_pulses=len(sid.keys())

        # USRP DC offset bug due to truncation instead of rounding.
        # Ryan Volz has a fix for firmware in USRPs.
        # note that this appears to change as a function of time
        # we can probably only estimate this from the estimated autocorrelation functions 
        z_dc=n.complex64(-0.212-0.221j)

        bg_samples=[]
        bg_plus_inj_samples=[]
        z_dc_samples=[]

        sidkeys=list(sid.keys())

        A=[]
        mgs=[]
        mes=[]
#        sigmas=[]
        idxms=[]
        rmins=[]

        sample0=800
        sample1=7750
        rdec=rg
        m0=int(n.round(sample0/rdec))
        m1=int(n.round(sample1/rdec))

        n_meas=m1-m0

        for li in range(n_lags):
            # determine what is the lowest range that can be estimated
            # rg0=(gc - txstart - 0.6*pulse_length + lag)/range_decimation
            rmin=int(n.round((sample0-111-480*min_tx_frac+lags[li])/rdec))
    #        rmin=int(n.round(lags[li]/rdec+sample0/rdec+480/rdec/2))# half a pulse length needed
            cm=convolution_matrix(n.zeros(m1),rmin=rmin,rmax=rmax)
            rmins.append(rmin)
            idxms.append(cm["idxm"])
            A.append([])
            mgs.append([])
            mes.append([])
#            sigmas.append([])                


        # start at 3, because we may need to look back for GC
        for keyi in range(3,n_pulses-3):

            t0=time.time()
            key=sidkeys[keyi]

            if sid[key] not in tmm.keys():
                print("unknown pulse code %d encountered, halting."%(sid[key]))
                exit(0)

            z_echo=None
            zd=None

            z_echo = d_il.read_vector_c81d(key, 10000, channel) - z_dc
            
            # no filtering of tx to get better ambiguity function
            z_tx=n.copy(z_echo)

            if sid[key] == 300:
                if use_long_pulse == False:
                    # ignore long pulse
                    continue
                # if long pulse, then take the next long pulse
                next_key = sidkeys[keyi+3]
                z_echo1 = d_il.read_vector_c81d(next_key, 10000, channel) - z_dc
            elif sid[key] == sid[sidkeys[keyi+1]]:
                # if first AC, subtract next one
                next_key = sidkeys[keyi+1]
                z_echo1 = d_il.read_vector_c81d(next_key, 10000, channel) - z_dc

            elif sid[key] == sid[sidkeys[keyi-1]]:
                # if second AC, subtract previous one.
                next_key = sidkeys[keyi-1]
                z_echo1 = d_il.read_vector_c81d(next_key, 10000, channel) - z_dc

                if debug_gc_rem:
                    plt.plot(zd.real+2000)
                    plt.plot(zd.imag+2000)
                    plt.plot(z_echo.real)
                    plt.plot(z_echo.imag)            
                    plt.title(sid[key])            
                    plt.show()



            noise0=tmm[sid[key]]["noise0"]
            noise1=tmm[sid[key]]["noise1"]
            last_echo=tmm[sid[key]]["last_echo"]
            tx0=tmm[sid[key]]["tx0"]
            tx1=tmm[sid[key]]["tx1"]
            gc=tmm[sid[key]]["gc"]
            e_gc=tmm[sid[key]]["e_gc"]        

            # filter noise injection.
            z_noise=n.copy(z_echo)
            z_noise=lpf.lpf(z_noise)

            # the dc offset changes
            z_dc_noise=n.mean(z_noise[(last_echo-500):last_echo])
            z_dc_samples.append(z_dc_noise)
            bg_samples.append( n.mean(n.abs(z_noise[(last_echo-500):last_echo]-z_dc_noise)**2.0) )
            bg_plus_inj_samples.append( n.mean(n.abs(z_noise[(noise0):noise1]-z_dc_noise)**2.0) )

            z_tx[0:tx0]=0.0
            z_tx[tx1:10000]=0.0

            # normalize tx pwr
            z_tx=z_tx/n.sqrt(n.sum(n.real(z_tx*n.conj(z_tx))))
            z_echo[last_echo:10000]=0.0
            z_echo1[last_echo:10000]=0.0

            z_echo[0:gc]=0.0
            z_echo1[0:gc]=0.0

            z_echo=lpf.lpf(z_echo)
            z_echo1=lpf.lpf(z_echo1)

            zd=z_echo-z_echo1

            zd[0:gc]=n.nan
            z_echo[0:gc]=n.nan
            z_echo[last_echo:10000]=n.nan
            zd[last_echo:10000]=n.nan        
            t1=time.time()
            read_time=t1-t0
            t0=time.time()
            for li in range(n_lags):
                for ai in range(lag_avg):
                    amb=decim.decimate(z_tx[0:(len(z_tx)-lags[li+ai])]*n.conj(z_tx[lags[li+ai]:len(z_tx)]))

                    # gc removal by the T. Turunen subtraction of two pulses with the same code, transmitted in
                    # close proximity to one another.
                    measg=decim.decimate(zd[0:(len(z_echo)-lags[li+ai])]*n.conj(zd[lags[li+ai]:len(z_echo)]))

                    # no gc removal
                    mease=decim.decimate(z_echo[0:(len(z_echo)-lags[li+ai])]*n.conj(z_echo[lags[li+ai]:len(z_echo)]))

                    TM=amb[idxms[li]]
                    TM=sparse.csc_matrix(TM[m0:m1,:])

                    mgs[li].append(measg[m0:m1])
                    mes[li].append(mease[m0:m1])
                    A[li].append(TM)
            t1=time.time()
            ambiguity_time=t1-t0
            print("prep %d/%d ambiguity time %1.2f read time %1.2f (s)"%(keyi,n_pulses,ambiguity_time,read_time))            

        acfs_g=n.zeros([rmax,n_lags],dtype=n.complex64)
        acfs_e=n.zeros([rmax,n_lags],dtype=n.complex64)    
        acfs_g[:,:]=n.nan
        acfs_e[:,:]=n.nan    

        acfs_var=n.zeros([rmax,n_lags],dtype=n.float32)
        acfs_var[:,:]=n.nan

        noise=n.median(bg_samples)    
        alpha=(n.median(bg_plus_inj_samples)-n.median(bg_samples))/T_injection 
        T_sys=noise/alpha

        for li in range(n_lags):
            print(li)

            AA=sparse.vstack(A[li])
            #print(AA.shape)
            mm_g=n.concatenate(mgs[li])
            mm_e=n.concatenate(mes[li])
            sigma_lp_est=n.zeros(len(mm_g))
            sigma_lp_est[:]=1.0

            # remove outliers and estimate standard deviation 
            if True:
                print("ratio test")
                mm_gm=n.copy(mm_g)
                mm_em=n.copy(mm_e)
                n_ipp=int(len(mm_gm)/n_meas)
                mm_gm.shape=(n_ipp,n_meas)
                mm_em.shape=(n_ipp,n_meas)

                sigma_lp_est=n.sqrt(n.percentile(n.abs(mm_em[:,:])**2.0,34,axis=0)*2.0)
                sigma_lp_est_g=n.sqrt(n.percentile(n.abs(mm_gm[:,:])**2.0,34,axis=0)*2.0)            

                ratio_test=n.abs(mm_em)/sigma_lp_est
                ratio_test_g=n.abs(mm_gm)/sigma_lp_est_g

                localized_sigma=n.abs(n.copy(mm_em))**2.0
                wf=n.repeat(1/10,10)
                WF=n.fft.fft(wf,localized_sigma.shape[0])
                for ri in range(mm_em.shape[1]):
                    # we need to wrap around, to avoid too low values.
                    localized_sigma[:,ri]=n.roll(n.sqrt(n.fft.ifft(WF*n.fft.fft(localized_sigma[:,ri])).real),-5)

                # make sure we don't have a division by zero
                msig=n.nanmedian(localized_sigma)
                if msig<0:
                    msig=1.0
                localized_sigma[localized_sigma<msig]=msig


                if False:
                    plt.pcolormesh(localized_sigma.T)
                    plt.colorbar()
                    plt.show()

                    plt.pcolormesh(ratio_test.T)
                    plt.colorbar()
                    plt.show()
                    plt.pcolormesh(ratio_test_g.T)
                    plt.colorbar()
                    plt.show()

                debug_outlier_test=False
                if debug_outlier_test:
                    plt.pcolormesh(mm_em.real.T)
                    plt.colorbar()
                    plt.show()


                # is this threshold too high?
                # maybe 6-7 might still be possible.
                mm_em[ratio_test > 10]=n.nan
                mm_gm[ratio_test_g > 10]=n.nan

                # these will be shit no matter what
                mm_em[localized_sigma > 100*msig]=n.nan
                mm_gm[localized_sigma > 100*msig]=n.nan            

                if debug_outlier_test:            
                    plt.pcolormesh(mm_em.real.T)
                    plt.colorbar()
                    plt.show()

                    plt.pcolormesh(localized_sigma.T)
                    plt.colorbar()
                    plt.show()

                sigma_lp_est=localized_sigma
                sigma_lp_est.shape=(len(mm_g),)

                mm_gm.shape=(len(mm_g),)
                mm_em.shape=(len(mm_e),)
                mm_g=mm_gm
                mm_e=mm_em

            mm_g=mm_g/sigma_lp_est
            mm_e=mm_e/sigma_lp_est

            gidx = n.where( (n.isnan(mm_e)==False) & (n.isnan(mm_g)==False) & (n.isnan(sigma_lp_est) == False) )[0]
            print("%d/%d measurements good"%(len(gidx),len(mm_g)))

            srow=n.arange(len(gidx),dtype=int)
            scol=n.arange(len(gidx),dtype=int)
            sdata=1/sigma_lp_est[gidx]

            Sinv = sparse.csc_matrix( (sdata, (srow,scol)) ,shape=(len(gidx),len(gidx)))

            # take outliers and bad measurements
            AA=AA[gidx,:]
            mm_g=mm_g[gidx]
            mm_e=mm_e[gidx]        

            try:
                t0=time.time()
                # we should probably do a
                # AA=n.dot(AA,Sinv)
                # first. this would save all the Sinv dot products. no time to test and validate this now
                # 
                # A^H diag(1/sigma)
                AT=n.conj(AA.T).dot(Sinv)
                # A^H S^{-1} A (Fisher information matrix)
                ATA=AT.dot(n.dot(Sinv,AA)).toarray()

                # A^H \Sigma^{-1} m_g with ground clutter mitigation
                # note that 1/sigma is taken earlier when forming mm_g and mm_e
                # here we add a 1/sigma to get 1/sigma^2 on the diagonal of Sigma^{-1}
                ATm_g=AT.dot(mm_g)
                # A^H \Sigma^{-1} m_e no ground clutter mitigation
                # note that 1/sigma is taken earlier when forming mm_g and mm_e
                ATm_e=AT.dot(mm_e)

                # error covariance
                Sigma=n.linalg.inv(ATA)

                # ML estimate for ACF lag without ground clutter mitigation measures in place
                xhat_e=n.dot(Sigma,ATm_e)

                # ML estimate for ACF lag with ground clutter mitigation measures            
                xhat_g=n.dot(Sigma,ATm_g)

                t1=time.time()
                t_simple=t1-t0        
                print("simple %1.2f"%(t_simple))
                acfs_e[ rmins[li]:rmax, li ]=xhat_e
                acfs_g[ rmins[li]:rmax, li ]=xhat_g

                acfs_var[ rmins[li]:rmax, li ] = n.diag(Sigma.real)
            except:
                traceback.print_exc()
                print("something went wrong.")

        # plot real part of acf

        plt.pcolormesh(mean_lags,rgs_km[0:rmax],acfs_e.real,vmin=-1e6*(rg/120.0),vmax=1e7*(rg/120.0))
        plt.xlabel("Lag ($\mu$s)")
        plt.ylabel("Range (km)")
        plt.colorbar()
        plt.title("%s T_sys=%1.0f K"%(stuffr.unix2datestr(i0/sr),T_sys))
        plt.tight_layout()
        plt.savefig("%s/lpi-%d.png"%(output_prefix,i0/sr))
        plt.close()
        plt.clf()

        ho=h5py.File("%s/lpi-%d.h5"%(output_prefix,i0/sr),"w")
        ho["acfs_g"]=acfs_g
        ho["acfs_e"]=acfs_e    
        ho["acfs_var"]=acfs_var
        ho["rgs_km"]=rgs_km[0:rmax]
        ho["lags"]=mean_lags/sr
        ho["i0"]=i0/sr
        ho["T_sys"]=T_sys     # T_sys = alpha*noise_power
        ho["alpha"]=alpha     # This can scale power to T_sys (e.g., noise_power = T_sys/alpha)   T_sys * power/noise_pwr = T_pwr
        ho["z_dc"]=n.median(z_dc_samples)
        ho["pass_band"]=pass_band        # sort of important to store this, as this defines the low pass filter  
        ho["filter_len"]=filter_len      #
        ho.close()

if __name__ == "__main__":

    if True:
        datadir="/mnt/data/juha/millstone_hill/isr/2023-09-05/usrp-rx0-r_20230905T214448_20230906T040054/"
        #datadir="/media/j/fee7388b-a51d-4e10-86e3-5cabb0e1bc13/isr/2023-09-05/usrp-rx0-r_20230905T214448_20230906T040054"
        # E-region analysis
        lpi_files(dirname=datadir,
                  avg_dur=10,  # n seconds to average
                  channel="zenith-l",
                  rg=30,       # how many microseconds is one range gate
                  output_prefix="lpi_e",
                  min_tx_frac=0.4, # how much of the pulse can be missing
                  reanalyze=False,
                  filter_len=10,
                  pass_band=0.1e6,
                  maximum_range_delay=7000
                  )

    if False:
        # F-region analysis
        lpi_files(dirname="/media/j/fee7388b-a51d-4e10-86e3-5cabb0e1bc13/isr/2023-09-05/usrp-rx0-r_20230905T214448_20230906T040054",
                  avg_dur=10,  # n seconds to average
                  channel="zenith-l",
                  rg=120,       # how many microseconds is one range gate
                  output_prefix="lpi_f2",
                  min_tx_frac=0.1, # of the pulse can be missing
                  pass_band=0.1e6, # +/- 50 kHz 
                  filter_len=10,    # short filter, less problems with correlated noise, more problems with RFI
                  reanalyze=False)

    if False:
        # F-region analysis
        lpi_files(dirname="/media/j/fee7388b-a51d-4e10-86e3-5cabb0e1bc13/isr/2023-09-05/usrp-rx0-r_20230905T214448_20230906T040054",
                  avg_dur=10,  # n seconds to average
                  channel="zenith-l",
                  rg=240,       # how many microseconds is one range gate
                  output_prefix="lpi_ts",
                  min_tx_frac=0.0, # of the pulse can be missing
                  pass_band=0.1e6, # +/- 50 kHz 
                  filter_len=10,    # short filter, less problems with correlated noise, more problems with RFI
                  reanalyze=True)
        

